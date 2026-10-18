# 🤖 Agents-Core

**Specialist context, reusable workflows and repository memory for AI assistants.**

Agents-Core is a local Model Context Protocol (MCP) server for assistants that need a specialist role, a repeatable task procedure or context from earlier work in a repository. It supplies personas, domain skills, shared rules and optional reasoning patterns called cognitive implants. Your current model uses that context and executes the task with its existing tools.

- [Start here as an agent](docs/agent-guide.md): task-to-tool guide, first-call examples and operating limits.
- [Compact documentation index](llms.txt): direct Markdown links for agents discovering the project.
- [Install in an MCP client](llms-install.md): manual stdio configuration, including Cline.
- [Documentation map](docs/README.md): current guides, reference material, and evaluation reports.
- [AI contributor instructions](AGENTS.md) and [Claude instructions](CLAUDE.md).
- [Model workflows](flows/README.md): reusable Markdown instructions, including
  the [documentation refresh process](flows/documentation-refresh.md).

## When to use Agents-Core

| Your task needs | Agents-Core provides | Entry point |
|---|---|---|
| A specialist approach to implementation, review, architecture or writing | A task-relevant persona with selected skills and rules | `route_and_load` or `get_agent_context` |
| A repeatable PR review, documentation refresh or other repository procedure | Markdown workflows for your current model to execute | `list_flows`, then `run_flow` |
| A bounded assignment for a reviewer or parallel worker | A compact role, acceptance criteria and a structured result contract | `get_subagent_context`, then `validate_subagent_result` |
| Context from earlier work on the same project | Repository description and interaction history | `describe_repo` and `read_history` |
| Shared exact inputs across workers | Persistent artifacts and pinned manifests | [Shared artifacts](docs/shared-artifacts.md) |

The core server needs no external API key. Setup requires Python 3.11+ and downloads a local embedding model (about 1.1 GB with the default configuration). Clients connect through standalone stdio or the shared local service; the Claude Code plugin launches a separately installed server. Native worker execution, file edits and shell commands remain the host client's responsibility. See the [agent guide](docs/agent-guide.md) for selection examples, data handling and limits.

---

## 🚀 Quick Start

### One-command install (macOS and Linux)

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
  ([model switch on update](#model-switch-on-update)); otherwise setup writes the
  one default model (see [Environment Variables](#environment-variables));
- an existing `.venv` is reused and its dependencies are refreshed; it is
  recreated only when its Python version is unknown or older than 3.11;
- the Claude instruction and routing-reminder prompts are accepted. Client
  registration and Codex instructions never ask (see [After Cloning](#after-cloning));
- sync between machines is not asked about: the summary says how to turn it on,
  and the `AGENTS_USER_SYNC_*` variables set it up without questions
  ([Sync between machines](#sync-between-machines)).

Without a terminal the confirmation is skipped. To inspect the script first,
download it, read it, then run `bash install.sh`. Pass `init_repo.sh` flags with
`... | bash -s -- --skip-index`; `--skip-mcp` leaves client configurations and
instructions unchanged, and `--force-claude-code-mcp` registers Claude Code's own
entry even when the [Claude plugin](#claude-plugin) serves it.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_HOME` | `~/.agents-core` | Install directory |
| `AGENTS_REPO_URL` | `https://github.com/IEZhu/Agents.git` | Repository for a fresh clone |
| `AGENTS_BRANCH` | `main` for a new clone; a rerun keeps the checked-out branch | Branch to install: `main` takes releases, `dev` every merged pull request. A new `.env` gets it as `AGENTS_AUTO_UPDATE_BRANCH`, so both updaters follow it; an existing `.env` that names another branch keeps it, with a warning ([update channel](#update-channel)) |
| `AGENTS_ASSUME_YES` | unset | `1`, `true` or `yes` skips the confirmation, as does `... \| bash -s -- --yes` |
| `AGENTS_USER_SYNC_REPO`, `AGENTS_USER_SYNC_REMOTE` | unset | Set up [sync between machines](#sync-between-machines) without questions, with the variables listed there |

The installer never uses `sudo`. It refuses a non-empty `AGENTS_HOME` that is not
an Agents-Core checkout.

Rerunning it updates an existing checkout: it fast-forwards the checked-out
branch from the checkout's `origin` (or checks out `AGENTS_BRANCH` when that is
set), then reruns setup, which refreshes dependencies in `.venv` and the indexes
in place, and warns when `.env` follows another branch than an explicit
`AGENTS_BRANCH`. Without `AGENTS_BRANCH` a rerun keeps the checked-out branch
(a detached HEAD gets `main`) and never switches channels: that is
`channel set`'s job, with its downgrade check ([update channel](#update-channel)).
First close the client sessions that run this installation's stdio server, as
for any manual Git operation or rebuild. If this checkout serves the shared macOS service, update it with
`.venv/bin/python -m src.daemon update` instead (see [Updates](#updates)).
An update is refused when the checkout has uncommitted changes to tracked files
(commit or stash them first). Untracked files do not block an update and are left
in place; `git pull --ff-only` aborts without touching them if an incoming file
would overwrite one.

For step-by-step prompts, or on Windows, use [After Cloning](#after-cloning).

### Claude plugin

[`plugins/agents-core`](plugins/agents-core/README.md) is a Claude plugin that registers this
installation's MCP server for Claude Code and Cowork on your computer. It needs the local
installation above. While the plugin is installed and enabled, setup and `migrate` write no
`Agents-Core` entry for Claude Code, so the server is not registered twice, and still write the
routing instructions and the other clients' entries (no `--skip-mcp` needed).
[docs/claude-plugin.md](docs/claude-plugin.md) describes how it is validated and released.

Install the plugin from this repository's own marketplace in Claude Code (macOS or Linux):

```text
/plugin marketplace add IEZhu/Agents
/plugin install agents-core@agents-core
```

For a fresh setup, add the plugin before running the server installer above, so setup sees the
plugin and avoids a duplicate Claude Code registration. If the server was installed first,
follow the [plugin setup guide](plugins/agents-core/README.md#setup) to remove the duplicate entry.
The plugin is a launcher; marketplace installation does not install its Python environment or
embedding model. See [distribution and submissions](docs/distribution.md) for other catalogs,
and [llms-install.md](llms-install.md) for manual stdio setup in Cline and compatible clients.

### Uninstall

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
  Optional session readiness hooks and Desktop's `Agents-Readiness` entry are removed only
  when their generated settings still match and their interpreter belongs to this installation's
  `.venv`. Their copied runtimes are removed only when unchanged and no remaining handler
  references them. Modified entries, files and adapters from another installation are listed
  as kept so you can remove them manually.
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
outside known remote-tracking history, or ignored files outside the installer's `.env` file and `.venv/`
directory), and
nothing that is or contains a filesystem root, the home directory, `~/Documents` or a profile
or XDG base directory is ever removed. Backups are private (0700 directories, 0600 files). A
checkout you cloned yourself and run the script from is kept when it is separate from the
installation being removed. The commit check uses existing local remote-tracking refs without
fetching; it does not confirm that the remote is still available. `.env` and `.venv/` are
intentional installation assets and can be removed, so keep personal files elsewhere. Other
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

### Claude Code cloud sessions

To have Agents-Core in [Claude Code cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web),
create a cloud environment whose setup script runs
[`scripts/setup_cloud_env.sh`](scripts/setup_cloud_env.sh) and whose network
allows Hugging Face. The [cloud environment guide](docs/cloud-runs.md#cloud-environment-with-agents-core)
lists the settings.

### After Cloning

Use Python 3.11 or newer, as required by [pyproject.toml](pyproject.toml).
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
  global instructions without asking; this does not connect Codex to MCP (see
  [Codex instruction installation](#codex-instruction-installation));
- targets another profile when `CLAUDE_CONFIG_DIR` (Claude Code),
  `AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` or
  `AGENTS_ANTIGRAVITY_MCP_CONFIG` is exported in the shell that runs setup. Setup
  does not read them from `.env`, and a set variable also counts as detecting
  that client (see [alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations));
- on macOS, once the shared service is installed, keeps these clients on the
  service instead of writing standalone stdio entries. Do not rerun setup while
  the service runs (see [service updates](docs/shared-mcp-daemon.md#updates-and-recovery));
- asks once, at the end, whether to set up [sync between machines](#sync-between-machines),
  unless it is set up already; with `--yes` or without a terminal it does not ask,
  and the summary says how to turn it on.

`--skip-mcp` skips all client changes, the instruction updates included. With the
[Claude plugin](#claude-plugin) installed and enabled, setup skips only Claude Code's
`Agents-Core` entry; `--force-claude-code-mcp` (or `AGENTS_FORCE_CLAUDE_CODE_MCP=1`)
writes it anyway. Use `./scripts/init_repo.sh --help` for the
available options (`--yes` accepts all defaults). For the shared macOS service and
client connections, continue with [MCP client configuration](#-mcp-client-configuration).

For an existing installation, refresh Codex, Claude and existing Antigravity plugin instructions with
`python3 scripts/install_instructions.py`. This standalone command needs Python
3.11 or newer and does not rerun setup; see the [instruction update options](#codex-instruction-installation).

### Manual Setup

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

## ⚙️ Configuration

### Environment Variables

The core router needs no external API key. Configure `.env` using
[env.example](env.example). Setup creates `.env` from it, so a new `.env` starts
with these values:

```env
# Optional: set both keys to enable Langfuse tracing. Keep comments on their own
# line: older python-dotenv reads "KEY=  # text" as the value "# text".
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
ANTHROPIC_API_KEY=sk-ant-...    # Optional: for document OCR
AGENTS_DEBUG=0                  # Set to 1 for per-call JSON debug logs
# Optional local generation, disabled when unset; absolute owner JSON path
# AGENTS_LOCAL_INFERENCE_CONFIG=/absolute/local-inference.json
```

Both keys set enable tracing, so leave them empty to run without Langfuse. The
placeholders `pk-lf-...` and `sk-lf-...`, which an `.env` copied from an older
`env.example` may still hold, do not count as keys. The optional
[document OCR server](src/mcp_servers/document_ocr/README.md) needs a real
`ANTHROPIC_API_KEY`.

Embeddings run locally through FastEmbed (ONNX Runtime); the initial model download
requires network access. The default model is `microsoft/harrier-oss-v1-270m`
(MIT, multilingual, about 1.1 GB to download and 0.85 GB loaded): on the project's
retrieval sets it ranks implants and skills better than the earlier defaults
([results](docs/embedding-models-eval-results.md)). It loads from a plain-file
copy of the onnx-community export, pinned in
[embedding_prompts.py](src/engine/embedding_prompts.py), which also loads offline
once downloaded. Setup no longer asks: harrier-270m is the one model for every
machine, replacing the earlier Full, Balanced and Light choices. Setup writes it
to `EMBEDDING_MODEL` in `.env`, together with `EMBEDDING_MODEL_GENERATION`. When
`EMBEDDING_MODEL` is unset, for example after `--skip-index`, standalone stdio
uses it as well. Any FastEmbed model stays selectable,
`intfloat/multilingual-e5-large` included: edit `EMBEDDING_MODEL` and, with
server sessions stopped, run
`.venv/bin/python -m src.reindex`. An `.env` without
`EMBEDDING_MODEL_GENERATION=2`, such as one copied by hand from `env.example`,
needs that line too; otherwise the next start switches it to the default
([model switch](#model-switch-on-update)). The [shared macOS service](#shared-macos-service)
pins its model in its own configuration.

#### Model switch on update

Earlier setups pinned the model chosen then (Full e5-large, Balanced multilingual
MiniLM or Light English MiniLM). The first idle start after an update to this
version moves an `.env` from any of these, or any other model, to harrier-270m
once: it rewrites `EMBEDDING_MODEL`, keeps the old value in a comment, and
records `EMBEDDING_MODEL_GENERATION=2`
([model_migration.py](src/model_migration.py)). The server restarts itself
into the new model, downloads it in the background and re-embeds the skill,
implant and history indexes; the router cache resets. Until the indexes are ready,
tools answer `warming_up`. Rerunning `init_repo.sh` or `init_repo.bat` applies the
same switch and downloads the model during setup. A model set after the switch,
for example e5-large again, stays: the generation marker is already current. An
`EMBEDDING_MODEL` exported in a standalone server's environment overrides `.env` as before.
The shared service switches in its update transaction instead
([daemon guide](docs/shared-mcp-daemon.md#embedding-model)).

| Setting | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `microsoft/harrier-oss-v1-270m` when unset | Standalone embedding model |
| `EMBEDDING_MODEL_GENERATION` | Unset counts as `1`; setup and the model switch write `2` | Model generation the `.env` reached; an older one switches to the default model once ([model switch](#model-switch-on-update)) |
| `EMBEDDING_PROMPTS` | `on` | Model-specific query and passage prompts ([embedding_prompts.py](src/engine/embedding_prompts.py)); `off` embeds text as given |
| `EMBEDDING_BATCH_SIZE` | `4` | Documents per embedding batch (1–256) when history, skill or implant indexes are built. Memory grows with batch size, by up to about 68 MB per 512-token document with `intfloat/multilingual-e5-large` (a full harrier-270m index build peaked at about 2.8 GB with the default in the [recorded run](docs/embedding-models-eval-results.md)); larger values may be faster on machines with spare memory. Inputs are also capped at 2048 tokens |
| `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Persistent model cache; the shared service's `install` reads it only from the shell ([daemon guide](docs/shared-mcp-daemon.md)) |
| `AGENTS_CLIENT_REPO_ROOT` | Unset: a tool call's `workspace` inside the client's MCP roots, else the nearest `.git` or `CLAUDE.md` at or above `CLAUDE_PROJECT_DIR` or the working directory ([rules](#-repository-memory)) | Explicit stdio memory and workflow target. Refused with `workspace_unsafe` when it is a system, program or home directory. Set it in a per-project MCP entry's `env`, never in the installation `.env`, a shared registration or the Desktop entry |
| `RULES_ENABLED` | `1` | Include [shared rules](rules/README.md) in loaded context |
| `AGENTS_COOPERATION_ENABLED` | `0` | Opt the installed daemon into the local [agent cooperation service and mailbox](docs/agent-cooperation/README.md); HTTP and existing stdio-bridge clients share it; standalone stdio returns `store_unavailable` |
| `AGENTS_COOPERATION_RETENTION_DAYS` | `30` | Owner retention window for terminal cooperation receipts/events (integer, minimum 30); invalid values leave coordination unavailable; see [maintenance and recovery](docs/agent-cooperation/runtime.md) |
| `AGENTS_USER_RULES_DIR` | `data/user-rules` in the installation | Machine-local cache for [URL-subscribed user rules](docs/user-rules.md), separate from flow sync |
| `AGENTS_RULE_AUTH_HOSTS` | Unset | JSON map from a Bearer-token environment variable name to its exact permitted source hostname, required when `auth_env` is used for a user rule |
| `AGENTS_RULE_PRIVATE_HOSTS` | Unset | Exact hostnames allowed to resolve to RFC1918 IPv4 for user-rule fetches; comma or whitespace separated; loopback/link-local remain denied |
| `INTENT_CLASSIFIER_ENABLED` | `0` | Enable the optional intent-based enrichment classifier |
| `AGENTS_USER_FLOWS_DIR` | `flows/.user` in the installation | Location (absolute path) of [personal and repository flows](flows/README.md#personal-and-repository-flows), their persona choices, and the installation's rule, skill and implant switches (`components.json`) |
| `AGENTS_GITHUB_HOST` | `github.com` | GitHub host that [library sync](docs/user-sync.md#github) signs in to and calls; set it for GitHub Enterprise Server |
| `AGENTS_GITHUB_CLIENT_ID` | The Agents-Core OAuth App | Client ID of an OAuth App with Device Flow enabled for [library sync](docs/user-sync.md#github) sign-in; set it for a fork or GitHub Enterprise Server |
| `WARMUP_WAIT_SECONDS` | `20` | Seconds (1–600) a retrieval tool waits for background startup (stores, embedding model, rules) before it answers `warming_up`; the MCP handshake never waits ([details](docs/routing_flow.md#startup-and-readiness)) |

Routing thresholds and enrichment settings are defined in
[src/engine/config.py](src/engine/config.py). Its environment overrides, such as
`ROUTER_SIMILARITY_THRESHOLD`, `SKILLS_RELEVANCE_THRESHOLD`,
`IMPLANTS_RELEVANCE_THRESHOLD`, the
[`IMPLANT_*` settings](implants/README.md#1-agent-preferences-and-semantic-retrieval) and the
[intent classifier](docs/intent-classifier.md) thresholds in `env.example`, are
tuning and evaluation settings; normally keep their defaults. HTTP memory uses a
registered workspace header instead of `AGENTS_CLIENT_REPO_ROOT`.

### Sync between machines

Personal (`user:`) and repository (`repo:`) flows, persona choices and the
component switches live in the personal library (`flows/.user`, or
`AGENTS_USER_FLOWS_DIR`). Installation creates a managed local Git repository
inside that library, including with `--yes`, without a remote or network, and
without SSH tools. Each flow save, deletion and restoration records a local
version before returning; older `.history` snapshots are imported into Git.
Local history uses a fixed Agents-Core commit identity and never changes your
global Git configuration. Reinstalling preserves its commits and remote.

Sync keeps that library the same on your machines
through one private git repository per user, on macOS, Windows and Linux
([reference](docs/user-sync.md)). Remote sync is off until you set it up, and
connecting it reuses the local repository. A foreign repository in the library
is refused without changing it.

By default it syncs everything with a portable identity: personal flows, persona
choices, the switches, the flows' history, and the flows of every repository that
has an `origin` remote, work repositories included. Repository folders without an
`origin` and machine-local files stay on the machine. The wizard and the Sync page
compare the complete local library with an existing remote and offer **Merge libraries**
or **Replace local with repository**. The comparison lists identical, local-only,
remote-only and changed files, conflicts, history and paths that remain local.
The selected result needs confirmation before connection; discarded versions remain
restorable in private Git history and are excluded from later uploads.
`python -m src.user_sync scope --exclude repos/<key>` (or `--exclude-file PATH`)
later keeps a group or a file out on every machine without deleting it anywhere.

To set it up:

- **During setup.** `init_repo.sh` and `init_repo.bat` ask once, at the end:
  "Set up sync between your machines now? [y/N]". On macOS, when the shared
  service answers and serves the Sync page, yes opens its settings page there;
  otherwise it starts the terminal wizard. When the wizard has started sync on a
  machine without the service, setup also asks "Also sync every 5 minutes in the
  background? [Y/n]". Setup does not ask under `--yes` (which `install.sh` always
  passes), without a terminal, while git 2.32 or newer or `ssh-keygen` is missing,
  or when the library holds a `.git` that Agents-Core did not create, which it leaves
  alone. Updates never ask: `install.sh` runs setup with `--yes`, and the
  service's `update` and `auto-update` do not run it. Sync that is set up is only
  reported, and the summary says whether background sync is on.
- **Later**, from the installation root: `.venv/bin/python -m src.user_sync setup`
  (the terminal wizard), or the Sync page of `.venv/bin/python -m src.daemon flows-ui`
  with the shared service.
- **Without questions**, for example on a new machine: run
  `.venv/bin/python -m src.user_sync setup --from-env` with these variables in its
  environment. Setup reads them only there, never from `.env`. The installers honour
  them too, but the separate command keeps the token to that one command.

| Variable | Purpose |
|---|---|
| `AGENTS_USER_SYNC_REPO` | `OWNER/NAME` of an existing private GitHub repository, empty or holding the library |
| `AGENTS_USER_SYNC_REMOTE` | Instead of the repository: the SSH URL of a private repository on another host |
| `AGENTS_USER_SYNC_NAME`, `AGENTS_USER_SYNC_EMAIL` | Required: the commit identity; sync never reads your git configuration |
| `AGENTS_USER_SYNC_LABEL` | This machine's label (default: the platform and a random suffix) |
| `AGENTS_GITHUB_TOKEN` | Optional, for a GitHub repository: a token with the `repo` scope, which setup checks with GitHub, keeps in the OS secret store and uses to add this machine's deploy key |

From the installation root, with the token typed without echo and set for this
one command only:

```bash
read -rs t
AGENTS_GITHUB_TOKEN="$t" AGENTS_USER_SYNC_REPO=me/agents-library AGENTS_USER_SYNC_NAME="My Name" \
    AGENTS_USER_SYNC_EMAIL=me@example.com .venv/bin/python -m src.user_sync setup --from-env
unset t
```

In PowerShell on Windows, the token lives in the session's environment until you
remove it:

```powershell
$secure = Read-Host -AsSecureString "GitHub token"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$env:AGENTS_GITHUB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
$env:AGENTS_USER_SYNC_REPO = "me/agents-library"
$env:AGENTS_USER_SYNC_NAME = "My Name"
$env:AGENTS_USER_SYNC_EMAIL = "me@example.com"
.venv\Scripts\python.exe -m src.user_sync setup --from-env
Remove-Item Env:AGENTS_GITHUB_TOKEN
```

The command takes the token out of its own environment before anything else, so
none of its child processes (git, ssh, ssh-keygen) inherits it. When you give the
token to the installers instead, `install.sh` passes it to `init_repo.sh` only,
never to git, and `init_repo.sh` passes it to the sync step only. `init_repo.bat`
clears it, because cmd cannot keep a variable from its children: on Windows, use
the separate command. Never keep the token in `.env`.

Without a token or an earlier `github login`, setup prints this machine's public
key to add to the repository as a deploy key with write access; run it again
afterwards, and the installer's summary says so. Setup then checks access and
privacy and prints the preview. An existing remote with local content or saved
versions stops at `connection_choice_required`, even without conflicts. Review
`preview --connection-mode merge` or `preview --connection-mode replace`, then
run `start` with the same mode and the selected preview's `--confirm` hash.
An empty remote accepts only merge; an empty local library can download the
remote after the preview. Where privacy cannot be checked,
another host's key needs confirming, or the repository belongs to another owner,
it says to run it again with `--confirm-private`, `--trust-host-key` or
`--confirm-owner OWNER`. On a machine without the shared service
it then turns background sync on (`schedule enable`, every 5 minutes). A second
run with another repository replaces a setup that the variables made and that
never started, also one that stopped at a mistyped host; GitHub accepts a key on
one repository only, so setup first removes this machine's deploy key from the
old repository, or, without the API, gives this machine a new key and names the
old repository to clean up. Once sync has started, the variables change nothing.
A token GitHub refuses, or one that cannot see the repository, is reported with
the variable's name and the `repo` scope it needs. The commands setup prints say
where to run them: `cd <installation> && …` on macOS and Linux, `in <installation>,
run …` on Windows, where cmd and PowerShell chain commands differently.

GitHub sign-in in the wizard and on the Sync page is the OAuth device flow of the
Agents-Core OAuth App, so `gh` is not needed; its client ID ships in the code. A fork
or GitHub Enterprise Server sets `AGENTS_GITHUB_CLIENT_ID` in `.env` to an OAuth App of
its own with Device Flow enabled (see [Environment Variables](#environment-variables)).
Without sign-in, use `AGENTS_GITHUB_TOKEN`, or give an SSH URL and add the deploy key by
hand.

Changes reach your other machines when each machine syncs. With the shared
service, it syncs every few minutes and after each save; `install` says whether
sync is set up. Without it, the scheduled run (`.venv/bin/python -m src.user_sync
schedule enable`) syncs every 5 minutes; without that, a machine sends and
receives changes only in the cycle that follows each save made through its stdio
servers. A flow edited on two machines is merged line by line when the edits touch
different lines; otherwise both versions are kept as a [conflict](docs/user-sync.md#conflicts),
which the Sync page and the flow tools (`sync_conflicts`) report. The
[command reference](docs/user-sync.md#command-line) covers status, conflicts, pausing and
the scope.

Git also carries the permitted intermediate flow versions: after A → B → C on
one machine, another can restore B after syncing. Local history is kept on
private Git refs, while the remote publishes only permitted files and
versions; excluded repository groups and historical secrets are checked before
publication. A restoration creates a new version. Git is the only version
store; the interaction journal `history.md` retains its separate role.

### Updates

Feature PRs target `dev`; release PRs promote `dev` into `main`
([branches and releases](#branches-and-releases), with the release
[procedure](docs/releases.md#procedure)). Merged release PRs record the existing
UTC calendar version with an immutable Git tag at the resulting commit. See
[Calendar releases](docs/releases.md) for event guards, same-minute releases and
retry behavior.

The shared daemon has its own update controller. Run
`.venv/bin/python -m src.daemon update` for a manual update; automatic daemon
updates are opt-in through `.venv/bin/python -m src.daemon auto-update enable`.
See [service updates and recovery](docs/shared-mcp-daemon.md#updates-and-recovery).
While the service runs, do not update its checkout with `install.sh`, `git pull` or
`scripts/init_repo.sh`: they bypass drain, maintenance, index backup, probation
and rollback. The daemon disables the standalone updater described below.

#### Update channel

An installation follows one branch, its update channel: `main` takes releases,
`dev` every merged pull request ([branches and releases](#branches-and-releases)).
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
clean tracked tree, unchanged dependency manifests), the build while the service
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

#### Standalone stdio auto-update

The server can keep itself current. Updates are **two-phase** — prepared in the
background, activated on the next idle start — so the live install is never mutated
mid-session:

1. **Prepare** (background): a daemon thread (non-blocking, so it never delays
   serving) fetches the target branch and, if the install is fast-forwardable,
   builds the new version's vector stores in an isolated git worktree under
   `data/.prepared/<sha>`, then writes a marker. The live tree and stores are
   untouched.
2. **Activate** (next idle start): if a valid prepared update exists, the server
   fast-forwards the live tree (local, no network) and atomically moves the
   pre-built stores into `data/` — the expensive embedding already happened in
   phase 1. Activation runs only at a start with no other server session of this
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
- dependencies are **not** auto-installed. When `requirements.txt` or
  `pyproject.toml` changed, the updater only logs a warning. The staged updater
  warns while preparing, before the live checkout has the new manifests, and
  builds with the current environment, in which the activated code then also
  runs. For such an update, stop the server sessions and update manually:
  `git pull --ff-only`, `.venv/bin/pip install -r requirements.txt`,
  `.venv/bin/python -m src.model_migration .env` (applies a pending
  [model switch](#model-switch-on-update) before the rebuild), then
  `.venv/bin/python -m src.reindex`. If it has already activated, stop the
  sessions and install the dependencies before restarting. Only with
  `AGENTS_AUTO_UPDATE_STAGING=0` is the new `requirements.txt` already in place
  when the warning appears.

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
chosen revision, and successfully run `python -m src.reindex` there. Remove the
journal only after verifying the restored checkout and rebuilt stores, then
restart the server. An unreadable journal also requires this explicit recovery.

```env
AGENTS_AUTO_UPDATE=1                     # 0 to disable
AGENTS_AUTO_UPDATE_REMOTE=origin
AGENTS_AUTO_UPDATE_BRANCH=main           # the update channel: main (releases) or dev
AGENTS_AUTO_UPDATE_TIMEOUT=30            # seconds per git op
AGENTS_AUTO_UPDATE_INTERVAL=900          # throttle network checks (0 = every start)
AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT=3600  # seconds per index build (staged or in-place); stops only a hung build
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

## 🎯 How It Works

The shared daemon can also proxy manually configured HTTP MCP servers through the existing Agents-Core entry. Open **MCP** in the web UI, store credentials in the native OS secret store and select allowed apps. Definitions and per-host selections sync through the private personal library; tokens stay in each host's OS secret store. Tools appear as `<alias>__<tool>` after reconnecting MCP in open chats. See [HTTP MCP proxy](docs/mcp-proxy.md) for setup and the initial scope.

The server exposes MCP tools that any compatible client can call:

| Tool | Purpose |
|------|---------|
| `route_and_load(query, chat_history?, protocol_version=2, current_persona?)` | Semantic routing when selection is requested; returns a loaded role or candidates for the client to choose |
| `get_agent_context(agent_name, query, reasoning?, chat_history?, protocol_version=2, current_persona?, force_reload=False)` | Direct agent loading when the target is already known; the active agent returns `NO_CHANGE` unless `force_reload=True` restores lost instructions |
| `refresh_persona_context(query, current_persona, chat_history?)` | Refresh skills/implants for the active role without reselecting it |
| `get_subagent_context(agent_name, task, acceptance_criteria, ..., complexity?, client?)` | Prepare a compact task-scoped worker role, bounded components and a versioned result contract; with `complexity`, also the model to launch it on; [worker guide](docs/subagent-contract.md) |
| `validate_subagent_result(contract, result)` | Check the worker result against the parent-retained contract; structural validation only, without opening references; names the next model tier when a tiered worker fell short |
| `cooperation_register(...)` | Register bounded work in the optional local cooperation journal; does not spawn workers or grant repository access |
| `cooperation_read(...)` | Read a bounded repository snapshot and ordered cooperation events through the verified workspace |
| `cooperation_apply(...)` | Apply an authenticated, revision-checked cooperation action; journal claims coordinate participating agents, not arbitrary filesystem writes |
| `cooperation_send(...)` | Persist a bounded message for a participant in the same repository through the shared MCP mailbox |
| `cooperation_receive(...)` | Poll the authenticated participant's inbox; messages remain pending until acknowledged |
| `cooperation_ack(...)` | Acknowledge inbox delivery only; does not accept agreement terms, artifacts or new permissions |
| `cooperation_wait(...)` | Await private durable mailbox events for up to 30 seconds, or poll with timeout zero; resume with the notification cursor |
| `cooperation_request_status(...)` | Inspect one sent or received request's delivery, processing state, revision and deadline |
| `cooperation_request_update(...)` | Report processing or sender cancellation with a request revision and idempotent operation ID |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `list_flows(scope="all")` | Discover built-in, personal (`user:`) and repository (`repo:`) Markdown workflows, with IDs and content revisions |
| `run_flow(flow, request="", repo_path=None, current_persona?)` | Load a workflow for the caller's repository; returns `needs_execution` for the current model to carry out using its tools, plus `persona_activation` when the flow names a persona |
| `get_flow` / `save_flow` / `delete_flow` | Manage personal and repository flows from chat, with history and conflict detection; stored in the git-ignored `flows/.user` ([details](flows/README.md#personal-and-repository-flows)) |
| `set_flow_persona(flow, agent?, skills?, implants?, rules?, reset=False)` | Choose the agent and exact skills, implants and rules a flow runs with, without copying its text; stored in `flows/.user/personas/`. Without `agent` the flow runs without a persona; `reset=true` restores the flow's frontmatter ([details](flows/README.md#choose-a-flows-agent-and-components)) |
| `list_user_rules` / `get_user_rule` / `subscribe_user_rule` / `refresh_user_rule` | Inspect or explicitly fetch cached [URL-subscribed user rules](docs/user-rules.md); only subscribe and refresh contact the source |
| `set_user_rule_enabled` / `rollback_user_rule` / `delete_user_rule` | Change a user rule using its current revision, without fetching; private cached content stays outside flow sync |
| `log_interaction(agent_name, query, response_content, persona?, persona_action?, intent?, action?, outcome?, files?, tags?, request_id?, reasoning?)` | End-of-turn attribution log ([details](#-repository-memory)); `persona` is the last `SUCCESS`/`NO_CHANGE` descriptor object copied verbatim with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`), `persona_action` is `keep`, `switch`, `refresh` or `restore` and only goes with `persona`; `files`/`tags` are JSON arrays. Malformed attribution is written as `unverified` |
| `clear_session_cache()` | Stdio only: administrative reset of the shared enrichment cache; not required for persona changes. For HTTP, use `.venv/bin/python -m src.daemon clear-cache` |
| `describe_repo(repo_path=None, force_refresh=False)` | Read-only repository context and cached summary preview; returns `needs_summary` when a refresh is needed, without sampling or writing ([details](#-repository-memory)) |
| `write_repo_summary(summary, repo_hash, repo_path=None, workspace_id=None)` | Explicitly persists the summary after `needs_summary`; pass its `repo_hash`, `repo_path` and `workspace_id` back unchanged; refuses the running installation/source root |
| `read_history(limit?, since?, query?, machine?, offset?, entry_id?)` | Recent entries or lazy semantic recall over the action log, with long texts as previews that keep the result under Claude Code's 50,000-character limit; `offset` pages on, `entry_id` returns one entry whole; with [user library sync](docs/user-sync.md#repository-history), also the entries the user's machines shared, filtered by `machine` (a label, or `local`) |
| `artifact_publish` / `artifact_manifest_create` | Persist exact UTF-8 task data and pin selected artifact IDs/hashes in a task manifest; [artifact guide](docs/shared-artifacts.md) |
| `artifact_manifest_get` / `artifact_read` / `artifact_manifest_verify` / `artifact_manifest_release` | Retrieve an active pinned manifest, read bounded slices, independently verify stored bytes or revoke subsequent manifest access while retaining data |
| `local_inference_status()` | Lazy configuration/capability status for the optional local generation service; does not start a model or contact Redis |
| `local_context_create` / `local_context_grant` / `local_context_revoke` / `local_context_release` | Capture owner-authorized shared text/files, issue scoped bearer grants, revoke a child or release a context |
| `local_generate` / `local_generation_result` / `local_generation_cancel` | Submit one bounded completion, reconnect/poll its retained result, or cancel it; UUID deduplication prevents a repeated submit from generating twice |

For shared task inputs, publish once and pass the complete manifest to
`get_subagent_context.artifact_manifest`. Preparation pins it in the retained
contract and supplies exact reads for a native worker. The parent still validates
the unchanged result and independently verifies its artifact evidence. Optional
`local_context_create.artifact_manifest` instead hydrates every selected entry
exactly into the bounded local context before generation; it does not extend
model capacity or provide a tool loop. Artifacts persist in private service state
independently of Redis; manifest expiry/release ends access without deleting
bytes. See [persistent shared artifacts](docs/shared-artifacts.md) for workspace
capture boundaries, quotas and the complete workflow.

The optional [cooperation runtime](docs/agent-cooperation/README.md) is disabled by default. Its mailbox lets different AI MCP clients and model providers exchange messages through the same enabled installed HTTP daemon, including clients using the existing stdio bridge; it needs no native host messaging tool. Standalone stdio has no coordinator. Clients can await private durable events with `cooperation_wait`, then read and acknowledge messages. Request processing and delivery have separate states. Waiting completes an active call; automatic launch or wakeup of an idle model requires a host adapter. This source implementation has not been enabled in the live installation or evaluated with different provider models.

The server also registers MCP prompts (slash commands): `ask` routes like
`route_and_load`; one prompt per agent `routing.trigger_command` and per
`routing.aliases` entry loads that agent directly; `describe_repo` (optional
`force=true`) drives the repository memory bootstrap. Agent and `ask` prompts
accept `current_persona` as descriptor JSON. See the [protocol API](docs/routing_flow.md#api)
for parameters and responses.

### Workflows in the caller's repository

Ask the model to use Agents-Core to run `documentation-refresh` or `pr-review`
in the current repository. It calls `list_flows()` to discover flows and
`run_flow(flow="documentation-refresh")` to get the instructions. For review,
pass the PR/MR URL and constraints in `request`, such as `no-merge`.

The flow files stay in the MCP installation. Inspection, edits, tests and PR/MR
actions target the caller's repository. HTTP requires a registered workspace in
`X-Agents-Workspace`, even when `repo_path` is supplied. Stdio uses the project
resolved as described in [Repository Memory](#-repository-memory), and a workflow
fails instead of falling back to the installation. An optional `repo_path` must
stay within that workspace.

The tool reads and returns instructions; the current model executes them with
its existing permissions and tools. `needs_execution` does not mean the work is
complete. See the [workflow guide](flows/README.md#through-agents-core-mcp) for
the response contract, target resolution, and authoring rules.

Most built-in flows name a persona in their frontmatter: `pr-review` runs as
`code_reviewer`, `documentation-refresh` as `tech_writer`. For such a flow,
`run_flow` also returns `persona_activation`, a protocol 2 response: pass
`current_persona` to `run_flow` and apply the activation as a switch before
executing the flow. To choose another agent or exact skills, implants and rules
for a flow, use `set_flow_persona` or the flow editor's **Persona** panel
([details](flows/README.md#choose-a-flows-agent-and-components)).

Besides the built-in flows, users keep personal (`user:`) and per-repository
(`repo:`) flows in the installation's git-ignored `flows/.user`. Ask the model to
save, change, restore or delete one; it uses `get_flow`, `save_flow` and
`delete_flow`. A bare flow name resolves `repo:`, then `user:`, then the built-in.
With the shared daemon, the version link in the persona footer or
`.venv/bin/python -m src.daemon flows-ui` opens a local
[settings page](docs/shared-mcp-daemon.md#flow-editor); a browser of the OS user
running the daemon signs in by itself. It opens on an overview of the
installation: usage statistics, the AI apps that use it, a live diagram of a
request's path, and the model, directories and their sizes. The same page edits
flows, shows every agent with its metadata and prompt (read-only), and lists rules,
skills and implants and switches each one on or off for the installation.

### Persona continuity

For optional session readiness in every supported client (Claude Code, Codex,
Cursor, Antigravity and Claude Desktop), see [Session readiness](docs/session-readiness.md).
Install it separately using the [commands for your platform](docs/session-readiness.md#install);
Codex requires reviewing and trusting the new hook in `/hooks`. Cursor and
Antigravity request display in the first response; ordinary Claude Desktop Chat
uses an MCP first-response fallback because it has no native lifecycle hook.

For automatic emoji prefixes on task conversations in Claude Code, Codex and supported
Cursor surfaces, see [Thread status](docs/thread-status.md). Its separate opt-in installer
preserves the readiness hooks and documents each client's title-update limits.

Before each request, the model silently checks whether its active role fits the
task. If it does, it keeps that role without routing, candidate selection or
enrichment calls. A needed specialization change triggers routing; a known
requested role loads directly. Lost instructions restore the known role; a
justified refresh rebuilds its context without reselecting it.

The server returns separate persona, rules, skills and implants blocks, an
activation descriptor and an exact footer (the four labelled component lists, then
a plain `Agents-Core <version>` segment; under the daemon the version links to the
web UI). Every successful switch, restore or
refresh replaces all four blocks, including changed or removed rules, while
preserving higher-priority instructions, conversation facts, goals, permissions
and tool results. This is logical replacement: MCP cannot physically delete old
messages. No history or cache clearing is required. Persona tools never sample
the conversation's answer. The separately enabled local inference tools execute
bounded completions on an owned local engine; they do not replace the client's
reasoning or native worker spawning.

Protocol 2 is the only protocol since 2026-09-29 (installer default since
2026-09-26). Clients pass `protocol_version=2` (also the default); any other value
returns `ERROR` without loading a persona. [Client decisions](docs/routing_flow.md#client-decisions)
records the telemetry and evaluation evidence for the switch and links the
remaining validation gaps for each tested client/model.

Delegated workers use a separate [subagent contract](docs/subagent-contract.md):
the parent prepares an authored compact role with `get_subagent_context`, forwards
its unchanged `worker_context` through native spawning only after `status: prepared`, and validates every returned or revised JSON result with `validate_subagent_result` and the original retained contract. Require `valid: true` before accepting or integrating it; a failed or unavailable endpoint means work stays with the parent, without an unprepared worker or unvalidated result. Default packages
select up to three skills and zero implants under a 16,000-character context
budget. The parent then checks the evidence and acceptance criteria. The tools
do not launch a model or enforce its runtime permissions or token budget. A valid
worker package supplies the role without another full activation, chat footer or
per-result interaction log; ordinary conversation attribution remains unchanged.

[Model tiers](docs/subagent-model-tiers.md) cut flagship tokens on simpler
assignments. The parent rates each one `complexity: low | standard | high` and
names its host in `client`; the package's `execution.spawn` then launches the worker on the newest lighter model
the client offers: Claude Code `haiku` or `sonnet`, Codex's newest `*-luna` or
`*-sol` at medium effort, Cursor Auto or its newest Composer. `high` keeps the
parent's model. Risky or large assignments are raised to a higher tier, and a
worker that fails, stays partial or fails a check gets an `escalation` to the next
tier. `AGENTS_SUBAGENT_MODEL_<CLIENT>_<TIER>` overrides a model,
`AGENTS_SUBAGENT_TIERING=0` turns tiering off, and `AGENTS_SUBAGENT_LEDGER=1`
records outcomes for `scripts/subagent_tiers_report.py`.

With MCP available, a new ordinary conversation activates a persona before its first final
answer, even for a greeting or project-status question. `ROUTE_REQUIRED` must be
completed with `get_agent_context` in that turn. The client then composes the
answer with the exact bundle footer, calls `log_interaction`, and sends the answer.
`keep` applies only after a bundle has been activated and retained.

Antigravity shared-daemon user-scope setup and migration install this protocol in the
enabled plugin's `~/.gemini/config/plugins/agents-core/rules/AGENTS.md`, which
Antigravity loads as startup rules. Use `python3 scripts/install_instructions.py --clients antigravity` to refresh existing rules without changing its MCP connection, plugin registry or hooks, then begin a fresh Antigravity session. Use `migrate --clients antigravity` only when client connection or plugin setup also needs updating. The rules
file and workspace hook serve different steps: rules start persona activation;
the hook authenticates the project for memory calls. See
[Antigravity client setup](docs/routing_flow.md#installation-migration-and-rollback).

The checked-in `CLAUDE.md` uses the same template; the global installer does not
change this tracked file. After editing the template, refresh this checkout's
managed section:

```bash
.venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
```

On Windows, use `.venv\Scripts\python.exe` for the same command.
The helper replaces only the text between the markers and preserves repository
notes outside them. When the section changes, it leaves an untracked
`CLAUDE.md.backup.<timestamp>` in the checkout; do not commit it. Backup retention
and routing-reminder migration are described in
[installation, migration and rollback](docs/routing_flow.md#installation-migration-and-rollback).
Review your own project instructions and memory for conflicting unconditional
`route_and_load` requirements; these are not automatically rewritten.

Client instructions still written for protocol 1 no longer match this server:
their calls receive protocol 2 bundles they do not describe. Rerun the installer or `scripts/install_instructions.py` to replace them.
A client whose server predates protocol 2 reports the mismatch once and answers
without an activated persona until the server is updated.

See [routing, compatibility and migration](docs/routing_flow.md) and the
[client protocol](scripts/templates/routing-protocol-core.md) for the complete
contract. Behavioral support must be established for each client/model pair;
passing server tests alone does not establish it.

---

## 🏗️ Architecture

```
Agents/
├── .github/workflows/    # Windows installer CI and the issue-agent bridge
├── agents/               # Agent personas, discovered from system_prompt.mdc
│   ├── software_engineer/
│   │   └── system_prompt.mdc
│   ├── common/           # agent-schema.json (frontmatter contract)
│   └── schemas/          # Output schemas named in agent prompts
├── skills/               # Reusable knowledge chunks (RAG)
│   └── skill-*.mdc
├── implants/             # Cognitive reasoning strategies (RAG)
│   └── implant-*.mdc
├── rules/                # Shared directives (rule-*.mdc; see rules/README.md)
├── src/
│   ├── server.py         # FastMCP tools, prompts, and standalone entrypoint
│   ├── startup.py        # Installation leases and isolated stdio indexes
│   ├── self_update.py    # Standalone staged updates
│   ├── reindex.py        # Skill and implant index rebuild
│   ├── model_migration.py # One-time switch to the default embedding model
│   ├── version.py        # Agents-Core version shown in the persona footer
│   ├── flows.py          # Built-in flow catalog and run_flow bundles
│   ├── user_flows.py     # Personal and repository flows (flows/.user)
│   ├── flow_persona.py   # A flow's agent and exact components
│   ├── component_toggles.py # Installation-wide on/off switches for rules, skills and implants
│   ├── component_catalog.py # Read-only agent and component listing for the web UI
│   ├── client_paths.py   # Client configuration paths shared by installers and migration
│   ├── file_lock.py      # Stable sidecar file locks
│   ├── daemon/           # HTTP app, service control, workspaces, client migration, web UI and its sign-in
│   ├── mcp_servers/      # Optional document OCR server and the MCP server template
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── persona.py    # Protocol 2 activation, restore, and refresh
│   │   ├── persona_bundle.py # Fresh instruction blocks and bundle revision
│   │   ├── readiness.py  # Background retrieval startup (warming_up)
│   │   ├── skills.py     # Skill retrieval (vector search)
│   │   ├── implants.py   # Implant retrieval (vector search)
│   │   ├── rules.py      # Shared rule loading
│   │   ├── config.py     # Centralized configuration
│   │   ├── embedder.py   # FastEmbed wrapper (ONNX Runtime)
│   │   ├── embedding_prompts.py # Model prompts and extra model registrations
│   │   ├── vector_store.py # NumPy-based vector store
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   ├── intent.py     # Optional task classification
│   │   ├── fingerprint.py # Index compatibility fingerprint
│   │   ├── context.py    # Context retrieval (history formatting)
│   │   └── language.py   # Language detection
│   ├── memory/           # Repository summary, history, managed sections
│   ├── schemas/          # protocol.py (request, response, persona), tool_args.py (MCP argument types)
│   └── utils/
│       ├── prompt_loader.py
│       ├── debug_logger.py     # Optional JSON debug logging
│       └── langfuse_compat.py  # Optional Langfuse layer
├── bridge/               # Node stdio bridge to the shared HTTP service
├── scripts/              # Setup, validation, test, and maintenance commands
├── scripts/templates/    # Versioned client instruction templates and the issue-agent bridge workflow
├── tests/                # Deterministic and opt-in integration tests
├── evals/                # Routing, enrichment, and client dialogue evaluations
├── docs/                 # Guides and reference documents
├── flows/                # Markdown workflows served to caller repositories through MCP
├── data/                 # Installation indexes and leased stdio state (ignored)
├── install.sh            # One-command installer (macOS and Linux)
├── env.example           # Template for .env
├── pyproject.toml        # Python project metadata
└── requirements.txt
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge chunks (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Rules** | [Shared directives](rules/README.md) included in every loaded bundle when enabled, unless a flow persona names an exact set |
| **Router** | Semantic matching + caching for fast agent selection |
| **Persona bundle** | Versioned role instructions, rules, skills, and implants retained by the client |
| **Flows** | [Markdown workflows](flows/README.md) served to the caller's repository through `run_flow`, optionally with their own persona |
| **Memory** | Per-project summary and action history |

---

## 🔌 MCP Client Configuration

### Codex instruction installation

During the client setup stage, both installers automatically update Codex's global
instructions when `CODEX_HOME` is non-empty, the default `~/.codex` directory exists, or
the `codex` command is available. `CODEX_HOME` selects the target directory when
non-empty; otherwise the target is `~/.codex`. There is no additional Codex prompt.
`--skip-mcp` skips this instruction update along with client configuration.

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
connection on macOS; without the service, add the [standalone Codex entry](#codex-configtoml).
Refreshing client instruction files alone does not require restarting the daemon. Start fresh client sessions to load them. Cursor and standalone Claude Desktop receive the shared contract through MCP initialization; this instruction-only command does not manage a global rules file for them. After updating server-side initialization instructions, restart or reconnect the server and reconnect those clients to receive the new initialization response. Existing project-level rules can override global guidance, so update their managed sections with the same template when they restate an older contract.

### Shared macOS service

On macOS, one shared daemon can serve Codex, Claude Code, Cursor and Claude Desktop.
Before `install`, capture a baseline and stop this installation's stdio servers,
either by disabling Agents-Core in open clients or by ending those sessions;
`install` fails while any of them holds the installation lease. See
[installation and client migration](docs/shared-mcp-daemon.md#installation-and-client-migration).
The service pins the plain-file copy of `microsoft/harrier-oss-v1-270m`, which
`install` does not download: run setup with the default model, or set
`EMBEDDING_MODEL` to it in `.env` and run `.venv/bin/python -m src.reindex` with
stdio sessions stopped. `install --model intfloat/multilingual-e5-large` pins a
cached e5-large snapshot instead. Export a custom `FASTEMBED_CACHE_DIR` in the shell before `install`,
which does not read it from `.env`. Desktop and tracked project configurations use
the Node 22+ bridge. See the [prerequisites](docs/shared-mcp-daemon.md).
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
installing shell's (see [tools and PATH](docs/shared-mcp-daemon.md#tools-and-path)).

Global registrations provide routing. With Node recorded, the user entries of
Claude Code, Codex and Cursor register each session's project themselves, and the
service keeps the registrations it wrote up to date at every start
([details](docs/shared-mcp-daemon.md#registrations-kept-current)). Without Node,
project memory requires a registered workspace: register each clone or worktree
with `migrate --workspace`.
Migration manages private bearer headers and backups. Reconnect MCP in open
clients after migration. See [service operations](docs/shared-mcp-daemon.md) for
scope audits, updates, token rotation and rollback. A client that still starts
`src/server.py` over stdio is served by the service as well: the stdio server
hands its session to the bridge instead of loading a second engine (see
[stdio servers of a service installation](docs/shared-mcp-daemon.md#stdio-servers-of-a-service-installation)).

On Windows the same commands, run with `.venv\Scripts\python.exe`, install the
service as a hidden Task Scheduler task of the current user, with its private
state in `%USERPROFILE%\.agents-core`; `update` and automatic updates work as on
macOS. The controller of an older checkout refuses `update` on Windows: update
such a checkout once with `stop`, `git pull --ff-only` and `start`. See
[Windows](docs/shared-mcp-daemon.md#windows-task-scheduler).

Setup, migration and audit read `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` and
`AGENTS_ANTIGRAVITY_MCP_CONFIG` only from the environment of the command;
`--client-config CLIENT=/absolute/file` selects an explicit file (see
[alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations)).
For example:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude
.venv/bin/python -m src.daemon migrate --clients cursor --client-config cursor=/absolute/profile/mcp.json
.venv/bin/python -m src.daemon audit --workspace /absolute/path/to/project
```

The configurations below are for explicit standalone stdio use (including platforms
without the macOS service). To return from the shared service to stdio, run
`restore-clients` and then `uninstall` (see
[rollback](docs/shared-mcp-daemon.md#updates-and-recovery); to remove Agents-Core
altogether, see [Uninstall](#uninstall)); setup writes
standalone stdio registrations again only after `uninstall`. Replace both absolute
installation paths below (the one-command installer uses `~/.agents-core`; expand
`~`). Set `AGENTS_CLIENT_REPO_ROOT` to the client project when its launch
directory is not reliable.

### Claude Code (`.mcp.json` in project root)

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
then returns `warming_up`, so the call can be retried ([details](docs/routing_flow.md#startup-and-readiness)).
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
wrote. The shared desktop server still needs the `workspace` argument for its memory
and flow tools ([rules](#-repository-memory)).

### Cursor (`.cursor/mcp.json` in the client project)

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

### Codex (`config.toml`)

Setup installs Codex instructions but does not register this server; without an
entry, Codex follows the instructions' MCP-unavailable fallback. Add to
`~/.codex/config.toml` (or `$CODEX_HOME/config.toml`):

```toml
[mcp_servers."Agents-Core"]
command = "/absolute/path/to/Agents/.venv/bin/python"
args = ["/absolute/path/to/Agents/src/server.py"]
```

Set `AGENTS_CLIENT_REPO_ROOT` in an `[mcp_servers."Agents-Core".env]` table only in
a trusted project's `.codex/config.toml`. In the user-level file it would direct
every session's memory and flows to that one project.

### Generic stdio

```bash
source .venv/bin/activate
python src/server.py
# Server communicates via stdin/stdout using MCP protocol
```

---

## 🧠 Creating New Agents

1. Create directory: `agents/<agent_name>/`
2. Create `system_prompt.mdc` with frontmatter:

```yaml
---
identity:
  name: "my_agent"
  display_name: "My Agent"
  role: "Expert in X"
  tone: "Professional, Clear"
routing:
  domain_keywords: ["keyword1", "keyword2"]
  trigger_command: "/my_command"
core_skills: []             # required lists of skill IDs, may be empty
preferred_skills: []
capable_skills: []
---
# My Agent System Prompt

## Identity
You are an expert in X...
```

`identity.name` must match the directory name, or the agent fails to load. Use
lowercase letters, digits and underscores (`^[a-z0-9][a-z0-9_]*$`): the activation
descriptor accepts no other agent name.
`trigger_command` and each optional `routing.aliases` entry become MCP slash
prompts. See [Skill tiers](#skill-tiers) and the
[field reference](agents/README.md#agent-source-and-metadata), including the
optional `preferred_implants`.

Validate the frontmatter with `.venv/bin/python scripts/validate_agents.py`. It
checks required fields and ID format, not whether the listed files exist; a
missing core skill or preferred implant file can make activation return `ERROR`.
The MCP server discovers the agent on its next startup; restart the shared daemon
through its controller when using that transport. The schema is
[agents/common/agent-schema.json](agents/common/agent-schema.json).

### Skill tiers

Each agent picks its skills explicitly in frontmatter, in three tiers:

```yaml
core_skills: [skill-content-structure, skill-dev-clean-code]
preferred_skills: [skill-dev-debugging, skill-dev-performance]
capable_skills: [skill-dev-testing, skill-git-conventions]
```

- `core_skills` are always loaded, unless switched off for the installation in the web UI.
- `preferred_skills` join the semantic pool with their distance multiplied by a boost factor (0.7), so they win close matches.
- `capable_skills` join the same pool at their base distance.

Skills outside the three lists are never loaded for that agent, except when a flow's persona lists exact skills ([details](flows/README.md#choose-a-flows-agent-and-components)). Guidance that applies to every agent belongs in [`rules/`](rules/README.md), not in a skill.

---

## 🧠 Repository Memory

The server stores repository summaries and action history separately for each
client project.

Summary destination checks use filesystem identity as well as resolved paths,
including case variants and macOS volume aliases of the protected checkout.

- **`describe_repo`** — reads repository context without sampling or writing project files, including with `force_refresh=True`. Returns `up-to-date` with the cached summary preview, or `needs_summary` with a prompt, `repo_hash`, `repo_path`, `workspace_id` and an `instruction` for the calling model. A refresh is needed when the repository fingerprint changes (top-level names, first- and second-level directory names, key manifest contents and the head of `README.md`), the managed section is missing, or `force_refresh=True`. Both stdio and HTTP use the same read-only behavior.
- **`write_repo_summary`** — explicitly persists the caller's overview in the managed *Repository Memory* section of `CLAUDE.md` and updates its fingerprint cache. Follow the description's `instruction` before calling it. The running Agents-Core installation and runtime source root are protected: writes return `rejected` with a `live_installation` reason before creating a lock, summary or cache. Existing summaries remain readable there; keep a new overview in the answer or use a separate worktree and describe that target again. A worktree's default memory root can normalize to the main checkout. A nested worktree inside the authorized workspace can be selected with its explicit physical `repo_path`; for an external worktree, first set a per-project `AGENTS_CLIENT_REPO_ROOT` to its physical path and reconnect MCP, or register that physical root over HTTP. Ordinary project and explicitly selected worktree summaries retain their existing storage and freshness behavior.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured. A partial, malformed or mismatching `persona`, or `persona_action` without `persona`, never drops the turn: the entry is written with an `unverified`/`mismatch` persona line and the response carries `warnings`; a turn whose project does not resolve goes to the installation's fallback journal (see below) instead of being dropped (sinks stay best-effort: a full queue or a sink failure can still lose a write; a failed history write is reported as `history_last_error`, see below). It returns at once with a local `timestamp` (`YYYY.MM.DD HH:MM:SS`), which the final answer shows as its first line when the footer lists the `answer-timestamp` rule; both writes happen in the background and are drained on shutdown.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing. A listing stays under the 50,000 characters above which Claude Code saves a tool result to a file. `outcome` comes as a preview of up to 600 characters, `intent` and `action` of up to 300, `files` and `tags` keep up to 20 items, and a shortened entry gives each cut field's full length in `truncated` (characters, or items for a list). When that is still too much, the previews get shorter, and then the last entries are left out, counted in `omitted`; `offset` skips that many entries, and the `instruction` gives the `offset` and `limit` that read them; entries logged between two calls shift those pages. Semantic results carry `intent`, `action` and `outcome` like recent ones, without `files` and `metadata`. `read_history(entry_id=...)` returns one entry whole, also one that rotation moved to `history/YYYY-MM.md`. With [user library sync](docs/user-sync.md#repository-history) set up, a repository's top-level checkout (with an `origin`) reads its `history.md` and `history/*.md` merged with the entries the user's machines shared for that repository, including this machine's other checkouts: one per entry hash, the checkout's own copy first, ordered by time and read newest month first. Each entry carries `machine` (null for the checkout's own entries, else the label of the machine that wrote it); `machine=<label>` keeps that machine's entries and `machine=local` this checkout's own. `history.md` itself stays the checkout's journal. A machine shares a repository's entries only after its owner reviewed them with `python -m src.user_sync history export`: approval covers the repository's later entries on that machine and exactly the past entries listed, so another checkout that joins later waits for its own review. `python -m src.user_sync history share all` shares every repository's entries from that machine without a review instead, except repositories `history revoke` leaves out.

Over stdio, the project is `AGENTS_CLIENT_REPO_ROOT` when set; otherwise the
nearest `.git` or `CLAUDE.md` at or above the start directory. The start directory
is `CLAUDE_PROJECT_DIR`, which Claude Code exports to the servers it starts, or else
the launch directory. A `CLAUDE_PROJECT_DIR` without a marker is used as named. A
launch directory without a marker is refused with `workspace_required`: it says
nothing about the project. A root (including the override) that is a filesystem
root, the home directory, or a system or program directory is refused with
`workspace_unsafe`: on Windows the Windows directory, `%ProgramFiles%`,
`%ProgramFiles(x86)%`, `%ProgramData%` and `C:\Users` itself; on POSIX `/usr`,
`/var`, `/home`, `/Users` and similar, plus `/etc`, `/bin`, `/usr/bin` and the other
system subtrees (`/usr/local`, `/private/tmp` and macOS temporary directories stay
allowed). If the launch directory no longer exists, memory falls back to the
installation and workflows return an error.

One stdio process can also serve several sessions. The Claude desktop app starts the
servers in `claude_desktop_config.json` once, in `C:\Windows\System32` without
`CLAUDE_PROJECT_DIR`, and injects them into its Code-tab sessions; setup and `migrate`
deny that entry in Claude Code (see above), so those sessions use their own server.
Its `roots/list` answer lists the folders of every open session,
so neither the process nor its roots tell which project a call belongs to. The memory
and flow tools therefore take `workspace`, the absolute path of the caller's working
directory, which the routing instructions tell the model to pass. When the client
declares the MCP roots capability, `workspace` is checked on every call: it must lie
inside one of the client's roots, resolves like `CLAUDE_PROJECT_DIR` (a git worktree
to its main checkout, as Claude Code does for a worktree session) and is refused when
unsafe; `source` is then `workspace`. It takes precedence over `CLAUDE_PROJECT_DIR`
and the launch directory, never over `AGENTS_CLIENT_REPO_ROOT`, and is never pinned
for the process. A client without roots cannot vouch for the path, so the process
root applies and a refusal names `AGENTS_CLIENT_REPO_ROOT`. Set
`AGENTS_CLIENT_REPO_ROOT` only on a per-project registration, never on a shared or
Desktop one.

`log_interaction` and `read_history` results carry `workspace` (`root`, `source`) and
`pid`, and, after a failed history write, `history_last_error`
(`code=history_unwritable`, `errno`, `path`, `at`), which the model should mention once.
Over stdio their workspace errors, and those of `describe_repo`, `write_repo_summary` and
the flow tools, carry `pid` and `workspace_inputs` (`cwd`,
`claude_project_dir`, `agents_client_repo_root`, `client`, `roots`), and, in a server the
Claude desktop app started (client `local-agent-mode-<server>`), a `hint` that names its
`claude_desktop_config.json` and how its Code tab gets its own server; when the client
declares roots and `workspace` was omitted, the refusal asks for it and
`log_interaction` tells the model to retry once with it.
Over HTTP, memory requires the registered `X-Agents-Workspace` header;
the daemon does not infer a project from its working directory. The user-scope
entries of Claude Code, Codex and Cursor are a bridge per session that registers the
session's project and sends that header itself ([details](docs/shared-mcp-daemon.md#installation-and-client-migration)).
When no project resolves, `log_interaction` keeps the turn in the installation's
fallback journal (`unresolved/history.md` next to user sync's private state) with the
directory the client named as unverified metadata, and `read_history` reads the
entries kept for that directory; nothing is written to the named directory.
They move into a project only on request: `python -m src.daemon history adopt
--to /absolute/project --claimed <dir>`, or "Move to a project" on the History tab;
with user library sync they are shared like a repository's history
([details](docs/shared-mcp-daemon.md#memory-and-errors)). `describe_repo`,
`write_repo_summary` and `repo:` flows still return `workspace_required`,
`workspace_unsafe` or `workspace_invalid`, with a `remedy`; routing remains
available. For HTTP without that bridge, register the project with
`migrate --workspace /absolute/project` (see
[service operations](docs/shared-mcp-daemon.md#installation-and-client-migration))
and reconnect MCP; for stdio, set `AGENTS_CLIENT_REPO_ROOT` as described above.
Do not retry logging in a loop.

Source history and `CLAUDE.md` stay in the project. The summary hash is stored in
the project's `data/memory/`. Derived router and history indexes live in private
daemon state or, for standalone processes, in leased `data/stdio/` slots of the
installation (a temporary per-process directory on a file system without
byte-range locks). These indexes can be rebuilt without deleting the source
history.

See [service memory behavior](docs/shared-mcp-daemon.md#memory-and-errors) for the
current transport contract and [the original memory design](docs/memory-subsystem-spec.md)
for its rationale.

`log_interaction` stores the supplied prompt and response by default; callers can
provide curated `intent`, `action`, and `outcome` fields. This checkout ignores
`history.md` and `history/`. In a client project, check the ignore rules for
`history.md`, `history/`, `data/memory/` and the hidden lock files
(`.history.md.lock`, `.CLAUDE.md.lock`, `.agents-description.lock`) before
committing its action log.

---

## 📊 Observability

Selected routing, loading, retrieval, and memory operations are instrumented with
Langfuse. `log_interaction` records the answer and declared persona attribution in
the background: its response reports both sinks as `queued` (Langfuse `skipped`
while retrieval warms up), a failed history write appears as `history_last_error`
on later results, and Langfuse failures go only to the server log. Set real Langfuse keys
in `.env` to enable it; leave them empty for local-only operation (see
[Environment Variables](#environment-variables)). Set
`LANGFUSE_TRACING_ENABLED=false` when running checks that should not send traces.

---

## 🛠️ Development

Read [AGENTS.md](AGENTS.md) for contributor instructions, [the documentation
refresh process](flows/documentation-refresh.md) for documentation work, and
[tests/README.md](tests/README.md) for the test matrix.

Keep reusable model task instructions in `flows/` and list them in the
[workflow catalog](flows/README.md), which explains how to run them locally or
through `run_flow`.

### Branches and releases

Every pull request targets `dev`, the integration branch; a release is a pull
request from `dev` into `main`, merged with a merge commit and tagged with its
calendar version. [Calendar releases](docs/releases.md) is the release procedure:
the merge method, closing keywords, hotfixes, the rules for `dev` and the tag
workflow. Installations choose their branch with `python -m src.daemon channel set
main|dev` or the web UI's Updates section ([update channel](#update-channel)). The
`push` triggers of the CI workflows cover `main` and `dev`.

### Validation

Run these commands from the checkout root in the test environment described in
[tests/README.md](tests/README.md#environment) (`requirements.txt` plus
`-e '.[evals]'`). The environment that setup creates installs only
`requirements.txt` and cannot run the whole suite:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/validate_agents.py
```

The default pytest configuration excludes `slow` tests. Use
`LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -m '' -q` to
include them. Bridge changes also require `node --test bridge/test.mjs`.
The test configuration isolates derived indexes in temporary storage; tests that
load embeddings still need the selected model to be available.

### Debug Logging

Enable detailed per-call JSON logging when [running the server manually](#generic-stdio):

```bash
AGENTS_DEBUG=1 python src/server.py
```

Standalone debug logs are written under the client project's `logs/`; HTTP
debug logs are written under the daemon's private `debug/` directory. Files use
`{YYYY-MM-DD}/{HH-MM-SS.fff}_{uuid}_{tool}_{direction}.json`. Logging is disabled
unless `AGENTS_DEBUG=1` (or `true`).

`route_and_load`, `get_agent_context`, and `refresh_persona_context` record paired
`req` and `res` snapshots. Their `data.call_id` identifies one invocation, including
concurrent calls and early errors, independently of the protocol's request ID.
The request stores all effective arguments, preserving supplied history and
persona inputs. The response's `data.response` is the exact JSON string returned
to the client, including all four instruction blocks, the descriptor, and footer
on `SUCCESS`. These snapshots show what the server returned; they do not prove
that the client applied the instructions.

Persona snapshots run outside the asyncio event-loop thread, in request/response
order. Diagnostic failures leave the protocol response unchanged.

Debug files can contain complete prompts, instructions, and supplied chat
history. Standalone logs are never pruned; `logs/` is ignored only in this
checkout, so exclude it in the client project or delete the files after
diagnosis. The daemon keeps its debug files for at most seven days and 100 MiB.

---

## 📝 License

MIT
