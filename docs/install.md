# Install and connect Agents-Core

Source of truth for behavior: `install.sh`, `scripts/init_repo.sh`,
`scripts/init_repo.bat`, `scripts/install_instructions.py`, `src/client_paths.py`,
`src/mcp_registry.py`.

The sections that follow install Agents-Core on one machine: the one-command
installer, the Claude plugin, removal, cloud sessions, the step-by-step setup
after cloning, and a fully manual setup.

## One-command install (macOS and Linux)

Requires `git`, `curl`, and Python 3.11 or newer as `python3`, with `pip3` on
`PATH` (see [After Cloning](#after-cloning)).

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash
```

The script clones the repository to `~/.agents-core` (or updates an existing
checkout), then runs `scripts/init_repo.sh --yes` after a single confirmation.
`--yes` applies the defaults:

- an `EMBEDDING_MODEL` already set in `.env` is kept, except that an `.env`
  from before the current model generation moves to the default model once
  ([model switch on update](configuration.md#model-switch-on-update)); otherwise setup writes the
  one default model (see [Environment Variables](configuration.md#environment-variables));
- an existing `.venv` is reused and its dependencies are refreshed (in the
  environment it links to, after an [update delivered
  dependencies](install.md#standalone-stdio-auto-update)); it is recreated only when its
  Python version is unknown or older than 3.11;
- the Claude instruction and routing-reminder prompts are accepted. Client
  registration and Codex instructions never ask (see [After Cloning](#after-cloning));
- sync between machines is not asked about: the summary says how to turn it on,
  and the `AGENTS_USER_SYNC_*` variables set it up without questions
  ([Sync between machines](user-sync.md)).

Without a terminal the confirmation is skipped. To inspect the script first,
download it, read it, then run `bash install.sh`. Pass `init_repo.sh` flags with
`... | bash -s -- --skip-index`; `--skip-mcp` leaves client configurations and
instructions unchanged, and `--force-claude-code-mcp` registers Claude Code's own
entry even when the [Claude plugin](#claude-plugin) serves it.

To install merged development changes before a release, fetch the development bootstrap and select `--dev`:

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/dev/install.sh | bash -s -- --dev
```

`--dev` selects `dev` before clone/update, overrides `AGENTS_BRANCH`, and is consumed by the bootstrap. Without it, installation keeps the normal stable/default-branch behavior. A fresh `.env` follows the selected branch; an existing explicit update-channel setting is preserved with the existing warning. The flag does not include unmerged pull requests. A bootstrap downloaded from an older `main` may not yet recognize a new flag; the `dev/install.sh` URL above avoids that version mismatch. On Windows, clone/check out `dev` before running `scripts\init_repo.bat`; the batch initializer does not switch the checkout.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_HOME` | `~/.agents-core` | Install directory |
| `AGENTS_REPO_URL` | `https://github.com/IEZhu/Agents.git` | Repository for a fresh clone |
| `AGENTS_BRANCH` | `main` for a new clone; a rerun keeps the checked-out branch | Branch to install: `main` takes releases, `dev` every merged pull request. A new `.env` gets it as `AGENTS_AUTO_UPDATE_BRANCH`, so both updaters follow it; an existing `.env` that names another branch keeps it, with a warning ([update channel](install.md#update-channel)) |
| `AGENTS_ASSUME_YES` | unset | `1`, `true` or `yes` skips the confirmation, as does `... \| bash -s -- --yes` |
| `AGENTS_USER_SYNC_REPO`, `AGENTS_USER_SYNC_REMOTE` | unset | Set up [sync between machines](user-sync.md) without questions, with the variables listed there |

The installer never uses `sudo`. It refuses a non-empty `AGENTS_HOME` that is not
an Agents-Core checkout.

Rerunning it updates an existing checkout: it fast-forwards the checked-out
branch from the checkout's `origin` (or checks out `AGENTS_BRANCH` when that is
set), then reruns setup, which refreshes dependencies in `.venv` and the indexes
in place, and warns when `.env` follows another branch than an explicit
`AGENTS_BRANCH`. Without `AGENTS_BRANCH` a rerun keeps the checked-out branch
(a detached HEAD gets `main`) and never switches channels: that is
`channel set`'s job, with its downgrade check ([update channel](install.md#update-channel)).
First close the client sessions that run this installation's stdio server, as
for any manual Git operation or rebuild. If this checkout serves the shared service, update it with
`.venv/bin/python -m src.daemon update` instead (see [Updates](install.md#updates)).
An update is refused when the checkout has uncommitted changes to tracked files
(commit or stash them first). Untracked files do not block an update and are left
in place; `git pull --ff-only` aborts without touching them if an incoming file
would overwrite one.

For step-by-step prompts, or on Windows, use [After Cloning](#after-cloning).

## Claude plugin

[`plugins/agents-core`](../plugins/agents-core/README.md) is a Claude plugin that registers this
installation's MCP server for Claude Code and Cowork on your computer. It needs the local
installation above. While the plugin is installed and enabled, setup and `migrate` write no
`Agents-Core` entry for Claude Code, so the server is not registered twice, and still write the
routing instructions and the other clients' entries (no `--skip-mcp` needed).
[docs/claude-plugin.md](claude-plugin.md) describes how it is validated and released.

Install the plugin from this repository's own marketplace in Claude Code (macOS or Linux):

```text
/plugin marketplace add IEZhu/Agents
/plugin install agents-core@agents-core
```

For a fresh setup, add the plugin before running the server installer above, so setup sees the
plugin and avoids a duplicate Claude Code registration. If the server was installed first,
follow the [plugin setup guide](../plugins/agents-core/README.md#setup) to remove the duplicate entry.
The plugin is a launcher; marketplace installation does not install its Python environment or
embedding model. See [distribution and submissions](distribution.md) for other catalogs,
and [llms-install.md](../llms-install.md) for manual stdio setup in Cline and compatible clients.

## Uninstall

`scripts/uninstall.sh` (macOS, Linux) and `scripts/uninstall.ps1` (Windows) remove
Agents-Core from your account: everything in the inventory below, with a report of what was
kept and why:

```bash
~/.agents-core/scripts/uninstall.sh --dry-run   # list everything, grouped by kind; change nothing
~/.agents-core/scripts/uninstall.sh             # ask once, then remove
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 --dry-run   # Windows
```

- **Services**: the LaunchAgents `local.agents-core.<id>[.updater|.sync|.scheduler]` (the shared
  service, its auto-updater, user sync and the autodev scheduler), the `systemd --user` units and
  the crontab line of user sync, and the Task Scheduler tasks `agents-core-daemon-<id>[-updater]`
  and `agents-core-sync-<id>`, where `<id>` is the installation id or the name of a state
  directory it found. They are stopped first; a linked plist or unit is left loaded and reported.
- **Client registrations**: only the `Agents-Core` and `Agents-Core-Desktop` entries in Claude
  Code (`~/.claude.json`, also its per-project entries, and the deny rules in
  `~/.claude/settings.json`), Claude Desktop, Codex, Cursor and Antigravity (with its
  `agents-core` plugin), and in the `.mcp.json`, `.codex/config.toml` and `.cursor/mcp.json` of
  every workspace the service registered. Other servers and settings stay byte for byte.
  Readiness and thread-status hooks, and Desktop's `Agents-Readiness` entry, are removed only
  when their generated settings still match and their interpreter belongs to this installation's
  `.venv`. Their copied runtimes are removed only when unchanged and no remaining handler
  references them. Modified entries, files and adapters from another installation are listed
  as kept so you can remove them manually. Thread-status turn records are retained and reported.
- **Instructions**: only the section between the routing-protocol markers in
  `~/.claude/CLAUDE.md` and Codex's `AGENTS.md`, and the generated routing reminder in Claude's
  memory. A file left empty is deleted.
- **State and data**: the private state directories (`~/Library/Application Support/Agents-Core`,
  `~/.local/state/agents-core`, `%USERPROFILE%\.agents-core`, `%LOCALAPPDATA%\Agents-Core`) with
  their logs, tokens, backups and histories, the embedding cache `~/.cache/fastembed` (or the
  one `FASTEMBED_CACHE_DIR` names in the environment, `.env` or `service.json`), the autodev
  trusted clone `~/.local/share/agents-autodev` (a clone configured elsewhere only when it lies
  outside `~/Documents` and the checkouts and is the runner's bare repository; a working tree is
  only listed), the
  timestamped copies the installer saved next to the instruction files (other `*.backup.*`
  files are only listed), and the installation `~/.agents-core` (`AGENTS_HOME`).
- **Credentials**: the keychain item `agents-autodev-github` and `~/.config/agents-autodev`
  only after a second question, or with `--yes --include-credentials`; `--yes` alone keeps them.

Every edited file is copied first to `~/.agents-core-uninstall-backups/<time>/`, whose
`manifest.json` maps each copy to its original. `--yes` skips the question, `--keep-data` keeps
the user library (`flows/.user`), user sync's state and the fallback journal. The final report
lists what was removed, what was kept and why. A path is removed only when it is in this
inventory, is neither a symbolic link nor a junction, and resolves inside the home directory; a
directory that `AGENTS_HOME`, `AGENTS_SERVICE_DIR`, `FASTEMBED_CACHE_DIR` or
`AGENTS_USER_FLOWS_DIR` names is removed only when its content shows it is Agents-Core's (a
checkout `AGENTS_HOME` names is kept when it has uncommitted changes, untracked files, a branch
outside the published `main` and `dev` channels, commits from local refs, reflogs or HEAD
outside known remote-tracking history, or ignored files outside the installer's `.env` file, its
`.venv` (a directory, or a link once an update delivered dependencies) and the updater's `.venvs/`
environments), and
nothing that is or contains a filesystem root, the home directory, `~/Documents` or a profile
or XDG base directory is ever removed. Backups are private (0700 directories, 0600 files). A
checkout you cloned yourself and run the script from is kept when it is separate from the
installation being removed. The commit check uses existing local remote-tracking refs without
fetching; it does not confirm that the remote is still available. `.env`, `.venv` and `.venvs/`
are intentional installation assets and can be removed, so keep personal files elsewhere. Other
ignored material, including extra runtime caches, conservatively keeps the selected checkout.
Registered submodules and retained `.git/modules` repositories also keep the whole checkout,
because parent Git history does not establish ownership of nested private work.
Git safety inspections use a copy of the process environment without inherited `GIT_*`
overrides, while preserving all other entries; other uninstall commands inherit the normal
environment. Untracked-file detection overrides Git settings that hide these files, and the
selected checkout is inspected again immediately before removal, after confirmation. This is not a
transaction with other writers: serialize install, uninstall and direct checkout edits.
Also kept are the files Agents-Core wrote into your projects (`history.md`, `history/`, the Repository
Memory section of `CLAUDE.md`), which the report lists. The logic is `src/uninstall.py`.

## Claude Code cloud sessions

To have Agents-Core in [Claude Code cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web),
create a cloud environment whose setup script runs
[`scripts/setup_cloud_env.sh`](../scripts/setup_cloud_env.sh) and whose network
allows Hugging Face. The [cloud environment guide](cloud-runs.md#cloud-environment-with-agents-core)
lists the settings.

## After Cloning

Use Python 3.11 or newer, as required by [pyproject.toml](../pyproject.toml).
On macOS and Linux, setup uses the `python3` on `PATH`, which needs the `venv`
module, and requires `pip3` on `PATH`. It does not look for versioned names such
as `python3.12`, and the macOS Command Line Tools `python3` is too old. On
Windows, `init_repo.bat` tries `py -3`, `python` and `python3`.
An existing `.venv` must also use a supported version. If it uses an older Python,
choose to recreate it when the installer asks; setup stops if you decline.
On NixOS, enable `programs.nix-ld` first: setup adds its library directory to
`LD_LIBRARY_PATH` for the indexing run and in each MCP registration's `env`.
Without it, setup only warns, and NumPy can fail to load `libstdc++`. Commands you
run yourself (such as `src.reindex` or tests) and client entries you write by hand
need `LD_LIBRARY_PATH=/run/current-system/sw/share/nix-ld/lib` too; for a manual
Codex entry, set it in `[mcp_servers."Agents-Core".env]`.

```bash
git clone <repository-url>
cd Agents

# Run initialization script
./scripts/init_repo.sh
```

On Windows, run `scripts\init_repo.bat`. It accepts `--skip-env`, `--skip-index`
and `--skip-mcp`, and `--yes` (or `AGENTS_ASSUME_YES=1`), which there only stops the
sync question: the other prompts still ask, and take their defaults when stdin is
not a console.

The interactive script:

- creates `.env` from `env.example` (or adds keys missing from an existing one),
  creates `.venv/` and installs dependencies, writes the default embedding model
  to `.env` when none is set and downloads it, and builds the skill and implant
  indexes. When `.venv/` exists and you
  keep it (the default answer), dependencies are not reinstalled; answer `y` to
  recreate it, pass `--yes`, or run `.venv/bin/pip install -r requirements.txt`;
- registers Agents-Core without asking as a standalone stdio server in each
  detected client's user-level configuration (Claude Code `~/.claude.json`, Cursor
  `~/.cursor/mcp.json`, Claude Desktop `claude_desktop_config.json` as
  `Agents-Core-Desktop`, Google Antigravity `~/.gemini/config/mcp_config.json`), and
  with Claude Desktop present also configures Claude Code, which the app's Code tab
  runs, and denies the desktop entry in Claude Code's `settings.json`
  ([why](#claude-code-mcpjson-in-project-root)), after copying existing files to
  exclusively reserved `<file>.backup.<unique-suffix>` names. Repeated runs,
  including within the same second, preserve prior backups. If a backup fails,
  the affected file is not updated. Both Unix and Windows setup detect Antigravity through
  its configuration directory, an available `agy` command or the explicit path
  override below. An existing invalid JSON file is left unchanged and reported
  as a failed client update. Unix setup then exits with a nonzero status instead
  of continuing or reporting successful initialization; fix the file manually
  or restore a backup before retrying;
- asks before installing protocol 2 instructions in the global Claude
  configuration. When Codex is detected, it installs the same protocol in Codex's
  global instructions and registers its MCP connection without asking (see
  [Codex instruction installation](#codex-instruction-installation));
- offers readiness/persona-restore hooks and emoji thread-status hooks separately for detected supported clients; `--yes` accepts both, with `--skip-hooks`, `--skip-readiness` and `--skip-thread-status` available to opt out;
- offers the shared daemon and cross-client agent cooperation separately on macOS and Windows; `--yes` accepts both, while `--skip-daemon` and `--skip-cooperation` opt out independently;
- targets another profile when `CLAUDE_CONFIG_DIR` (Claude Code),
  `AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` or
  `AGENTS_ANTIGRAVITY_MCP_CONFIG` is exported in the shell that runs setup. Setup
  does not read them from `.env`, and a set variable also counts as detecting
  that client (see [alternate client configurations](shared-mcp-daemon.md#alternate-client-configurations));
- on macOS, once the shared service is installed, keeps these clients on the
  service instead of writing standalone stdio entries. Do not rerun setup while
  the service runs (see [service updates](shared-mcp-daemon.md#updates-and-recovery));
- asks once, at the end, whether to set up [sync between machines](user-sync.md),
  unless it is set up already; with `--yes` or without a terminal it does not ask,
  and the summary says how to turn it on.

`--skip-mcp` skips all client changes, the instruction updates included. With the
[Claude plugin](#claude-plugin) installed and enabled, setup skips only Claude Code's
`Agents-Core` entry; `--force-claude-code-mcp` (or `AGENTS_FORCE_CLAUDE_CODE_MCP=1`)
writes it anyway. Use `./scripts/init_repo.sh --help` for the
available options (`--yes` accepts all defaults). For the shared service and
client connections, continue with [Codex instruction installation](#codex-instruction-installation)
and the sections after it.

For an existing installation, refresh Codex, Claude and existing Antigravity plugin instructions with
`python3 scripts/install_instructions.py`. This standalone command needs Python
3.11 or newer and does not rerun setup; see the [instruction update options](#codex-instruction-installation).

## Manual Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp env.example .env
# Edit .env for any optional integrations. To use a model other than the
# default, set EMBEDDING_MODEL together with EMBEDDING_MODEL_GENERATION=2

# Download the model and build the indexes
python -m src.reindex
```

---

The sections that follow connect MCP clients to that installation: Codex's global
instructions, the shared macOS and Windows service, and the standalone stdio
entries for Claude Code, Cursor, Codex and any other stdio client.

## Codex instruction installation

During the client setup stage, both installers automatically update Codex's global
instructions and register Agents-Core in `config.toml` when `CODEX_HOME` is non-empty, the default `~/.codex` directory exists, or
the `codex` command is available. `CODEX_HOME` selects the target directory when
non-empty; otherwise the target is `~/.codex`. There is no additional Codex prompt.
`--skip-mcp` skips both instructions and MCP registration. Discovery does not require
the CLI when the profile directory exists. Standalone setup writes absolute interpreter and
server paths. The registration step preserves other settings and servers and
retains an existing Agents-Core HTTP or stdio bridge connection. Managed service
upkeep can then upgrade compatible owned entries as described below. Repeating
setup leaves an already current entry unchanged. Standalone setup creates a unique
`config.toml.backup.*` snapshot before changing an existing TOML file and replaces it atomically; shared-service migration uses its private restorable backup journal. Invalid TOML or
an unsupported inline MCP layout is left unchanged and setup reports an error;
convert inline MCP definitions to table syntax before retrying. A symlinked
`config.toml` is also refused; use a regular file before retrying. On NixOS,
setup includes the same per-process native library path as for other clients.

Registration does not test live connectivity. Standalone registration preserves
an existing HTTP/bridge entry even if its service is unavailable; repair the
shared service with its migration command below. Managed service upkeep refreshes
only compatible entries as described next.
If this installation already has a shared-service marker, a new Codex registration uses the service's existing migration under its control lock and recovery checks, selecting its stdio bridge (Node when available, otherwise Python). Unusable shared state stops registration without falling back to standalone; the service may be stopped while its client configuration is prepared. The subsequent managed-registration refresh upgrades compatible entries and their known layers together; owner overrides remain intact. Without a marker, setup uses standalone stdio.
After moving a standalone installation or its virtual environment, rerun setup
to refresh its absolute paths.

The installer updates a non-empty `AGENTS.override.md` when present; otherwise it
updates `AGENTS.md`. This matches Codex's global instruction precedence. Personal
instructions outside the Agents-Core markers are preserved. Project instructions
can override global guidance, so review any old project-level routing rules too.
Start a fresh Codex session after updating instructions. See the
[official Codex instruction guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

To refresh global Codex and Claude instructions and an existing Antigravity plugin's rules from the current checkout without rerunning setup:

```bash
python3 scripts/install_instructions.py
```

On Windows, use `py -3 scripts\install_instructions.py`. The command needs Python
3.11 or newer and only uses its standard library; no virtual environment is
required. Add `--clients codex`, `--clients claude` or `--clients antigravity` to select a client, or pass a comma-separated selection. By default it refreshes detected Codex and Claude profiles and an existing `~/.gemini/config/plugins/agents-core/rules/AGENTS.md`. An absent Antigravity rules file is skipped; this command does not provision or enable the plugin. It uses the installer's managed-section replacement and backup retention. It migrates an existing exact
generated Claude routing reminder and its entry in `memory/MEMORY.md` of the Claude
configuration directory (`$CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`),
but does not create an absent reminder. Like the installers, it reads
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` from its environment.

This command updates global instructions, that reminder and its memory-index
entry, and their managed backups. It does not install dependencies, change `.env`
or vector indexes, register MCP connections, or change
service configuration. The shared daemon migration below configures Codex's MCP
connection on macOS and Windows; without the service, add the [standalone Codex entry](#codex-configtoml).
Refreshing client instruction files alone does not require restarting the daemon while it serves the same protocol; if the files now hold a newer template than the running daemon, a conversation's first activation warns that they hold a newer protocol and names the restart command (`.venv/bin/python -m src.daemon restart`, run in the installation). Start fresh client sessions to load them. Cursor and standalone Claude Desktop receive the shared contract through MCP initialization; this instruction-only command does not manage a global rules file for them. After updating server-side initialization instructions, restart or reconnect the server and reconnect those clients to receive the new initialization response. Existing project-level rules can override global guidance, so update their managed sections with the same template when they restate an older contract.

## Shared macOS and Windows service

On macOS and Windows, one shared daemon can serve Codex, Claude Code, Cursor, Claude Desktop and Antigravity.
Before `install`, capture a baseline and stop this installation's stdio servers,
either by disabling Agents-Core in open clients or by ending those sessions;
`install` fails while any of them holds the installation lease. See
[installation and client migration](shared-mcp-daemon.md#installation-and-client-migration).
The service pins the plain-file copy of `microsoft/harrier-oss-v1-270m`, which
`install` does not download: run setup with the default model, or set
`EMBEDDING_MODEL` to it in `.env` and run `.venv/bin/python -m src.reindex` with
stdio sessions stopped. `install --model intfloat/multilingual-e5-large` pins a
cached e5-large snapshot instead. Export a custom `FASTEMBED_CACHE_DIR` in the shell before `install`,
which does not read it from `.env`. Desktop and tracked project configurations use
a stdio bridge: Node 22+ when recorded and available, otherwise the standard-library
Python bridge. See the [prerequisites](shared-mcp-daemon.md).

Normal setup offers the shared daemon and cross-client cooperation as independent options on macOS and Windows. The one-command installer passes `--yes`, accepting both; interactive `init_repo` asks separately. `--skip-daemon` skips installation and managed-client migration, while `--skip-cooperation` preserves the existing cooperation setting. Cooperation requires a shared daemon; it can be enabled on an existing service even when new daemon installation is skipped. `--skip-mcp` skips both choices, and `--skip-hooks` affects only lifecycle hooks. Linux keeps standalone MCP setup because this service manager supports macOS and Windows.

Accepting cooperation persists only `AGENTS_COOPERATION_ENABLED=1` in `.env`, including under `--skip-env`, and starts or gracefully restarts the selected service when necessary. Already-current setup preserves the service's port, model, token and running process. Daemon installation uses the model/cache settings supplied by setup or saved in `.env`, without exporting unrelated `.env` keys. The existing launchers select the pre-index model themselves; a shell model override is not a uniform cross-platform setup option. The daemon requires an already cached embedding model; `--skip-index` does not waive that prerequisite. Managed registrations are brought to the shared bridge without replacing custom, foreign or absent profiles, and an existing `registrations disable` setting is respected. If an accepted setup option fails, setup reports it, still runs Sync, summary and health checks, retains completed steps and exits nonzero at the end. Reconnect open client sessions after migration. `/health` reports cooperation as `ready`, `disabled` or `unavailable`, separately from ordinary daemon readiness.

Run controller commands from the installation checkout (`~/.agents-core`, or
`AGENTS_HOME`, for the one-command installer).

```bash
.venv/bin/python -m src.daemon install
.venv/bin/python -m src.daemon start
.venv/bin/python -m src.daemon migrate --clients codex,claude,cursor
.venv/bin/python -m src.daemon migrate --workspace /absolute/path/to/project
.venv/bin/python -m src.daemon migrate --clients desktop
```

`install` prints the absolute paths it records for `git`, `ssh-keygen`, `ssh-keyscan`
and, on macOS, `lsof`, preferring copies only an administrator can change, and warns
about one that only a directory the user can write holds. The service starts these
tools by those paths and runs with system directories in its `PATH`, not the
installing shell's (see [tools and PATH](shared-mcp-daemon.md#tools-and-path)).

Global registrations provide routing and automatic project binding. The user entries
of Claude Code, Codex and Cursor register each safe session directory themselves,
including an empty folder without repository markers. Node is optional: the bridge
uses the recorded Python when Node is unavailable. The service keeps compatible
managed registrations and their known client layers consistent at every start,
and setup refreshes them after MCP configuration while automatic upkeep is enabled
([details](shared-mcp-daemon.md#registrations-kept-current)). Ordinary empty
session folders need no per-project migration. Use `migrate --workspace` for an
intentional project pin, or when the client provides no usable session directory,
MCP root or trusted workspace hook.
Migration manages private bearer headers and backups. Reconnect MCP in open
clients after migration. See [service operations](shared-mcp-daemon.md) for
scope audits, updates, token rotation and rollback. A client that still starts
`src/server.py` over stdio is served by the service as well: the stdio server
hands its session to the bridge instead of loading a second engine (see
[stdio servers of a service installation](shared-mcp-daemon.md#stdio-servers-of-a-service-installation)).

On Windows the same commands, run with `.venv\Scripts\python.exe`, install the
service as a hidden Task Scheduler task of the current user, with its private
state in `%USERPROFILE%\.agents-core`; `update` and automatic updates work as on
macOS. The controller of an older checkout refuses `update` on Windows: update
such a checkout once with `stop`, `git pull --ff-only` and `start`. See
[Windows](shared-mcp-daemon.md#windows-task-scheduler).

Setup, migration and audit read `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` and
`AGENTS_ANTIGRAVITY_MCP_CONFIG` only from the environment of the command;
`--client-config CLIENT=/absolute/file` selects an explicit file (see
[alternate client configurations](shared-mcp-daemon.md#alternate-client-configurations)).
For example:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude
.venv/bin/python -m src.daemon migrate --clients cursor --client-config cursor=/absolute/profile/mcp.json
.venv/bin/python -m src.daemon audit --workspace /absolute/path/to/project
```

The configurations below are for explicit standalone stdio use (including platforms
without the shared service). To return from the shared service to stdio, run
`restore-clients` and then `uninstall` (see
[rollback](shared-mcp-daemon.md#updates-and-recovery); to remove Agents-Core
altogether, see [Uninstall](#uninstall)); setup writes
standalone stdio registrations again only after `uninstall`. Replace both absolute
installation paths below (the one-command installer uses `~/.agents-core`; expand
`~`). Set `AGENTS_CLIENT_REPO_ROOT` to the client project when its launch
directory is not reliable.

## Claude Code (`.mcp.json` in project root)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/server.py"],
      "env": {
        "AGENTS_CLIENT_REPO_ROOT": "/absolute/path/to/project"
      }
    }
  }
}
```

Over stdio the server answers the MCP handshake without waiting for its stores,
the embedding model or Langfuse; they load in the background, and a retrieval
tool called before that finishes waits up to `WARMUP_WAIT_SECONDS` (default 20) and
then returns `warming_up`, so the call can be retried ([details](routing_flow.md#startup-and-readiness)).
Claude Code gives a server 30 seconds to connect by default; raise it with the
`MCP_TIMEOUT` environment variable (milliseconds) if a slow machine still times out.
The Claude desktop app starts the servers of `claude_desktop_config.json` once, outside
any project, and injects them into the Code sessions it launches, which also start their
own from the Claude Code registry. `init_repo` (like `python -m src.daemon migrate
--clients desktop`) therefore names the desktop entry `Agents-Core-Desktop`, taking over
an older `Agents-Core` entry there, and denies its tools in Claude Code
(`permissions.deny`: `mcp__Agents-Core-Desktop__*` in `~/.claude/settings.json`, or the
`CLAUDE_CONFIG_DIR` profile's). Setup configures Claude Code with the desktop app even
when Claude Code is not installed on its own, since the Code tab runs it. A Code-tab session
then uses Claude Code's own `Agents-Core` for its project, while the app's chats keep
`Agents-Core-Desktop` (#231); the session still sees both servers' instructions, and
calls to the denied tools are refused. The desktop app reads the profile its own
environment selects: when setup runs with `CLAUDE_CONFIG_DIR`, the app must see the
same value. While the shared service is installed, setup keeps the entries `migrate`
wrote. The shared desktop server selects a sole current MCP root automatically;
with several roots its memory and flow tools need the caller's root-verified
`workspace` argument ([rules](memory.md)).

## Cursor (`.cursor/mcp.json` in the client project)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/server.py"],
      "env": {
        "AGENTS_CLIENT_REPO_ROOT": "/absolute/path/to/project"
      }
    }
  }
}
```

## Codex (`config.toml`)

Setup installs Codex instructions and registers this server automatically. For a
manual standalone registration, add to
`~/.codex/config.toml` (or `$CODEX_HOME/config.toml`):

```toml
[mcp_servers."Agents-Core"]
command = "/absolute/path/to/Agents/.venv/bin/python"
args = ["/absolute/path/to/Agents/src/server.py"]
```

Set `AGENTS_CLIENT_REPO_ROOT` in an `[mcp_servers."Agents-Core".env]` table only in
a trusted project's `.codex/config.toml`. In the user-level file it would direct
every session's memory and flows to that one project.

## Generic stdio

```bash
source .venv/bin/activate
python src/server.py
# Server communicates via stdin/stdout using MCP protocol
```

## Updates

Feature PRs target `dev`; release PRs promote `dev` into `main`
([branches and releases](releases.md), with the release
[procedure](releases.md#procedure)). The root [`VERSION`](../VERSION) stores
one `YY.MM.DD.HHMM` calendar number for the runtime, Claude plugin manifest and
release tag. Prepare it in UTC with `python3 scripts/bump_plugin_version.py`
before promotion; later commits retain that number. The runtime adds `-dev` on
`dev` and `-dirty` for tracked uncommitted changes. Merged release PRs record
`vVERSION` with an immutable tag at the exact merge commit once the vulnerability
scan of that commit passes. See
[Calendar releases](releases.md) for preparation, legacy fallback, collision
checks and retry behavior.

The shared daemon has its own update controller. Run
`.venv/bin/python -m src.daemon update` for a manual update; automatic daemon
updates are opt-in through `.venv/bin/python -m src.daemon auto-update enable`.
See [service updates and recovery](shared-mcp-daemon.md#updates-and-recovery).
While the service runs, do not update its checkout with `install.sh`, `git pull` or
`scripts/init_repo.sh`: they bypass drain, maintenance, index backup, probation
and rollback. The daemon disables the standalone updater described below.

### Update channel

An installation follows one branch, its update channel: `main` takes releases,
`dev` every merged pull request ([branches and releases](releases.md)).
The setting is `AGENTS_AUTO_UPDATE_BRANCH` in the installation's `.env` (default
`main`), and both updaters read it: the stdio servers load `.env`, and the shared
service's controller (`update`, `auto-update run` and `status`, `channel`) reads
only the update channel keys from it. A variable exported in a shell overrides the
file for the commands started there, but only to steer the fast-forwards of a
checkout already on that branch: it never switches branches. `channel show`, `update`
and `auto-update status` say when the environment overrode `.env`.

```bash
.venv/bin/python -m src.daemon channel show       # setting and source, checkout, HEAD, remote tip, last update
.venv/bin/python -m src.daemon channel set dev    # follow dev from the next update on
.venv/bin/python -m src.daemon channel set main --allow-downgrade
```

Without the shared service, `.venv/bin/python -m src.update_channel show|set`
does the same. The web UI's overview (the version in its header) has an
Updates section that shows the version, the setting and the checked-out branch,
the scheduled updates and their last run, and saves `main` or `dev`. The CLI
accepts any branch the remote has, with a warning: feature branches get
rewritten and then stop fast-forwarding.

Saving changes no code. The next update applies it: `update`, a scheduled run, or,
without the service, a stdio server's next idle start. That update switches the
checkout only when it is on the previous setting, which `set` records as
`AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH`; a checkout on any other branch is local
development and stays untouched, and so is a checkout whose `.env` setting was
edited by hand without a previous record (`channel set` names the way out). The
switch runs through the usual update transaction, with its gates (idle service,
clean tracked tree, dependency changes it can deliver), the build while the service
serves, probation and rollback to the previous branch, with the target branch's
old tip and upstream settings restored. It waits while another worktree has the
target branch checked out. A switch to a branch that lacks non-merge commits of the installed HEAD,
for example `dev` → `main` before a release, would go back to older code that may
not read state or `.env` changes the newer code made: `set` refuses it until
`--allow-downgrade` or the UI's confirmation, which is recorded for that HEAD only
(`AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE`). A release's merge commit does not count, so
`main` → `dev` after a release needs no confirmation. `AGENTS_BRANCH=dev` with
`install.sh` installs `dev` and, for a new `.env`, writes it as the setting (an
existing one keeps its setting, with a warning).

### Standalone stdio auto-update

The server can keep itself current. Updates are **two-phase** — prepared in the
background, activated on the next idle start — so the live install is never mutated
mid-session:

1. **Prepare** (background): a daemon thread (non-blocking, so it never delays
   serving) fetches the target branch and, if the install is fast-forwardable,
   builds the new version's vector stores in an isolated git worktree under
   `data/.prepared/<sha>`, then writes a marker. A target that changes the
   dependencies first gets its own Python environment (see below). The live tree,
   stores and `.venv` are untouched.
2. **Activate** (next idle start): if a valid prepared update exists, the server
   fast-forwards the live tree (local, no network), atomically moves the
   pre-built stores into `data/` — the expensive embedding already happened in
   phase 1 — and points `.venv` at the target's environment when it has one.
   Activation runs only at a start with no other server session of this
   installation, including the shared daemon; otherwise the update stays pending.
   A start that overlaps activation waits and then loads the new code. Git and
   reindex children keep the installation leased until they and their
   descendants exit, so this wait can exceed the command timeout. Background
   preparation uses a separate lock and does not delay startup.

It is **safe by default**:

- follows `AGENTS_AUTO_UPDATE_BRANCH` (default `main`): it fast-forwards that
  branch, or switches to it from the previous setting ([update
  channel](#update-channel)) — a **no-op on feature branches**, so local development
  is never touched;
- only when the working tree is clean; a fast-forward (never merge or rebase), or
  a branch switch to the target branch's tip, which needs the recorded confirmation
  when that tip lacks commits of HEAD;
- a failed staged build discards the worktree and leaves the install as-is; crash
  recovery also uses the store's torn-pair detection and content-hash re-embed;
- staging and store paths must not contain symlinks; store moves are covered by
  the recovery journal below, so an interrupted move blocks startup instead of
  serving mixed indexes;
- offline/fetch/build failures leave the current version available. A failed
  activation merge (including a timeout after HEAD moved) restores the old tree.
  If rollback or re-exec fails, or an unexpected activation error leaves the tree's
  state unknown, startup stops instead of serving mixed versions;
- a dependency change gets a Python environment of its own. When the target
  changes a requirement line of `requirements.txt` (comments and blank lines do
  not count) or the environment contract of `pyproject.toml`
  (`project.dependencies`, `project.requires-python`, an existing
  `project.optional-dependencies` group, `build-system`), prepare builds
  `.venvs/<id>` beside the live environment with the same base interpreter:
  `python -m venv`, then `pip install` of the target's `requirements.txt`, its
  `pyproject.toml` runtime dependencies and the carried extras, within
  `AGENTS_AUTO_UPDATE_ENVIRONMENT_TIMEOUT` (default 1800 s). An extra is carried
  when both revisions declare it and every one of its distributions is installed
  in the active environment, also as a dependency of another package; it
  installs the target's requirements. httpx, for example, comes with mcp and
  anthropic, so the `local-inference` extra is carried and an automatically built
  environment also gets `httpx>=0.28.1,<0.29`. Plain requirements of one
  distribution from several places become one line with all their specifiers
  (`pytest>=7.0.0,<10,>=8.0`); lines with extras, markers or URLs stay as written. A
  complete environment with the same requirements is reused. The stores are
  built with it, and activation points `.venv` at it after the fast-forward and
  the store move, so the re-exec starts on it; client configurations keep naming
  `.venv/bin/python`. `.venv` thereby becomes a symbolic link: on POSIX the first
  switch renames the directory setup created to `.venvs/legacy-<UTC time>`. On
  Windows a `.venv` directory that setup created is refused (Windows cannot
  rename the directory of a running interpreter); only a `.venv` that is already
  a junction into `.venvs`, made by hand until setup creates that layout, is
  switched, from one junction to the next. The switch runs only while no session
  of the installation does, under the exclusive lease of an idle start: POSIX
  replaces the link atomically, while Windows removes the junction and then
  creates the new one. A failed build changes
  nothing and the next update tries again; a rollback after a failed merge,
  store move or switch points `.venv` back, and a successful activation removes
  the environments other than the active and the previous one. Packages
  installed by hand outside `requirements.txt`, `pyproject.toml` and its extras
  are not carried into a new environment. A `uv.lock`-only change needs no
  environment. The updater skips, with a logged reason naming the changed
  fields, a dependency change when the server was not started through
  `.venv/bin/python`; when `.venv` is a file or a link outside `.venvs`; when the
  base interpreter is missing, does not run or is another Python version than
  the one `.venv` was made with, or does not satisfy the target's
  `requires-python`; when `.venv` is a directory without `pyvenv.cfg`; on Windows
  while `.venv` is still the directory setup created; when a requirement line is
  a pip option (`-r`, `-e`, `--index-url` …), a local path, continues on the
  next line or has a version part other than `<operator><version>` clauses
  (`bad package`); when one distribution is pinned to two different versions
  (`==`); for changed `bridge/package.json` or
  `bridge/package-lock.json`; and for missing or malformed manifests. For those,
  stop the server sessions and update manually: `git pull --ff-only`, `.venv/bin/pip install -r requirements.txt`,
  `.venv/bin/pip install` of each `project.dependencies` entry of `pyproject.toml`
  that `requirements.txt` does not list and of the requirements of each extra in
  use (an automatic build installs all of them),
  `.venv/bin/python -m src.model_migration .env` (applies a pending
  [model switch](configuration.md#model-switch-on-update) before the rebuild), then
  `.venv/bin/python -m src.reindex`. With `AGENTS_AUTO_UPDATE_STAGING=0` every
  dependency change is skipped: the in-place path has no second environment to
  build or to roll back to.

Lifetime locks are `flock` on Linux and macOS and `LockFileEx` on Windows. On a
file system without byte-range locks (some Windows network shares) the server
runs with automatic updates disabled. When upgrading from a version that does
not hold these locks (on Windows, any version before #195), restart all existing
server sessions once. Manual Git operations and rebuilds must also be done with
those sessions stopped.

Windows differs in two places. A Windows lock belongs to the process that took
it, so a git or reindex child cannot keep the installation leased after the
server exits: the children run in a job object that ends them with the server,
and the next prepare starts over. A git child ended this way can leave a lock
file in `.git`; git names it when it next fails, and it can be deleted once no
git process runs. Windows also has no `exec`: after activation,
the server releases its leases, runs the updated code as a child process on the
same standard handles, and exits with the child's exit code, so the client keeps
talking to the process it started.

Before changing the live tree, the updater flushes a recovery journal to
`data/.update_in_progress.json`. It removes this guard only after successful
activation or rollback. An interrupted mutation or failed rollback keeps the
journal (and available staging artifacts); subsequent starts stop before importing
the application, even with auto-update disabled. To recover, stop the sessions,
inspect the recorded `old_sha` / `target_sha` and Git state, restore a complete
chosen revision with its Python environment (see below), and successfully run
`.venv/bin/python -m src.reindex` there. A recorded
`environment` names the Python environment switch, and the startup message
repeats it. To restore the old revision (`old_sha`), point `.venv` at
`.venvs/<environment.previous.name>` (or, when `previous.legacy` is set, remove
the `.venv` link and rename `.venvs/<previous.legacy>` back to `.venv`); when
`previous` is `{"kind": "directory"}` without a legacy name, `.venv` is still the
old environment and stays. To keep the target, which a checkout already at the
target with its stores moved requires, move a `.venv` directory aside first (to
`.venvs/<previous.legacy>`, or to any `.venvs/legacy-<UTC time>` name when none
is recorded), point `.venv` at `.venvs/<environment.to>` and rebuild the stores
with that interpreter: `.venv/bin/python -m src.reindex`. A checkout that runs
the target while `.venv` already satisfies every requirement the target names
(each distribution installed, in a release version that its specifiers accept;
markers are not evaluated, and a clause the updater cannot evaluate counts as
unmet) is not held: the updater logs a warning and continues on `.venv`. Remove the
journal only after verifying the restored checkout and rebuilt stores, then
restart the server. An unreadable journal also requires this explicit recovery.

```env
AGENTS_AUTO_UPDATE=1                     # 0 to disable
AGENTS_AUTO_UPDATE_REMOTE=origin
AGENTS_AUTO_UPDATE_BRANCH=main           # the update channel: main (releases) or dev
AGENTS_AUTO_UPDATE_TIMEOUT=30            # seconds per git op
AGENTS_AUTO_UPDATE_INTERVAL=900          # throttle network checks (0 = every start)
AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT=3600  # seconds per index build (staged or in-place); stops only a hung build
AGENTS_AUTO_UPDATE_ENVIRONMENT_TIMEOUT=1800  # seconds to build a dependency change's Python environment
AGENTS_AUTO_UPDATE_STAGING=1             # 0 = synchronous in-place update at an idle start
# AGENTS_AUTO_UPDATE_STAGING_DIR=/path   # staging parent (default data/.prepared; same filesystem as data/)
```

With `AGENTS_AUTO_UPDATE_STAGING=0` the updater falls back to the legacy in-place
path at an idle start under the same exclusive installation lock: fast-forward
the live tree and rebuild the stores right there, rolling back to the previous
commit if the rebuild fails. This mode can delay startup for fetch and reindex;
it no longer mutates files in a background thread while sessions are serving.

With serving processes stopped, run a manual rebuild using `python -m src.reindex`.

---
