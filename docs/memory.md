# Repository memory

Source of truth for behavior: `src/memory/`, `src/memory/describer.py`,
`src/memory/config.py`, `src/server.py`.

This file describes how the server stores a repository summary and an action
history for each client project, and how it selects that project.

The server stores repository summaries and action history separately for each
client project.

Summary destination checks use filesystem identity as well as resolved paths,
including case variants and macOS volume aliases of the protected checkout.

- **`describe_repo`** — reads repository context without sampling or writing project files, including with `force_refresh=True`. Returns `up-to-date` with the cached summary preview, or `needs_summary` with a prompt, `repo_hash`, `repo_path`, `workspace_id` and an `instruction` for the calling model. The prompt's context bundle (directory tree to depth 3, root manifests, first 200 lines of `README.md`) is capped at 24,000 shown units, the size Claude Code counts for a tool result, so the whole response stays under its 50,000-unit inline limit and is shown inline; a part over its cap is cut at a line boundary and ends with a note of what it left out. A refresh is needed when the repository fingerprint changes (top-level names, first- and second-level directory names, key manifest contents and the head of `README.md`), the managed section is missing, or `force_refresh=True`. Both stdio and HTTP use the same read-only behavior.
- **`write_repo_summary`** — explicitly persists the caller's overview in the managed *Repository Memory* section of `CLAUDE.md` and updates its fingerprint cache. Follow the description's `instruction` before calling it. The running Agents-Core installation and runtime source root are protected: writes return `rejected` with a `live_installation` reason before creating a lock, summary or cache. Existing summaries remain readable there; keep a new overview in the answer or use a separate worktree and describe that target again. A worktree's default memory root can normalize to the main checkout. A nested worktree inside the authorized workspace can be selected with its explicit physical `repo_path`; for an external worktree, first set a per-project `AGENTS_CLIENT_REPO_ROOT` to its physical path and reconnect MCP, or register that physical root over HTTP. Ordinary project and explicitly selected worktree summaries retain their existing storage and freshness behavior.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured. A partial, malformed or mismatching `persona`, or `persona_action` without `persona`, never drops the turn: the entry is written with an `unverified`/`mismatch` persona line and the response carries `warnings`; a turn whose project does not resolve goes to the installation's fallback journal (see below) instead of being dropped (sinks stay best-effort: a full queue, a log worker that cannot start or a sink failure can still lose a write; a failed history write is reported as `history_last_error`, see below). It returns at once with a local `timestamp` (`YYYY.MM.DD HH:MM:SS`), which the final answer shows as its first line when the footer lists the `answer-timestamp` rule; both writes happen in the background and are drained on shutdown.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing. A listing stays under the 50,000 characters above which Claude Code saves a tool result to a file. `outcome` comes as a preview of up to 600 characters, `intent` and `action` of up to 300, `files` and `tags` keep up to 20 items, and a shortened entry gives each cut field's full length in `truncated` (characters, or items for a list). When that is still too much, the previews get shorter, and then the last entries are left out, counted in `omitted`; `offset` skips that many entries, and the `instruction` gives the `offset` and `limit` that read them; entries logged between two calls shift those pages. Semantic results carry `intent`, `action` and `outcome` like recent ones, without `files` and `metadata`. `read_history(entry_id=...)` returns one entry whole, also one that rotation moved to `history/YYYY-MM.md`. With [user library sync](user-sync.md#repository-history) set up, a repository's top-level checkout (with an `origin`) reads its `history.md` and `history/*.md` merged with the entries the user's machines shared for that repository, including this machine's other checkouts: one per entry hash, the checkout's own copy first, ordered by time and read newest month first. Each entry carries `machine` (null for the checkout's own entries, else the label of the machine that wrote it); `machine=<label>` keeps that machine's entries and `machine=local` this checkout's own. `history.md` itself stays the checkout's journal. A machine shares a repository's entries only after its owner reviewed them with `python -m src.user_sync history export`: approval covers the repository's later entries on that machine and exactly the past entries listed, so another checkout that joins later waits for its own review. `python -m src.user_sync history share all` shares every repository's entries from that machine without a review instead, except repositories `history revoke` leaves out.

Over stdio, a non-empty `AGENTS_CLIENT_REPO_ROOT` selects the project as named
and remains authoritative. Without it, a client declaring MCP roots supplies the
current roots on each request. A sole distinct local root is selected automatically;
with several roots, pass the caller's absolute `workspace`, which must lie inside
a declared root. With no usable root, a model path alone cannot enable memory. No first or last root becomes a global process binding.
The verified directory resolves to the nearest `.git`, `CLAUDE.md` or `AGENTS.md`
at or above it, or to the directory itself when no marker exists; a git worktree
normalizes to its main checkout. This per-request selection reports `source` as
`workspace` and outranks the process's launch directory.

Antigravity's native workspace hook supplies an authenticated per-request path even
when standalone MCP starts from `/` without roots. Setup provisions the hook and
startup rules in the active native profile, with a private installation-bound
signing key; the MCP registration contains only its path. Both standalone and
shared-service transports verify the proof before repository tools run.

When the client does not declare MCP roots or supply a verified native hint, the start directory is an existing `CLAUDE_PROJECT_DIR`, or else
the launch directory. The nearest `.git`, `CLAUDE.md` or `AGENTS.md` at or above it owns
memory; without a marker, the safe existing session directory itself owns memory.
An ordinary empty folder therefore needs no marker creation, git initialization or
per-project migration. A model's unverified `workspace` argument cannot redirect a
process without roots. Set `AGENTS_CLIENT_REPO_ROOT` only on a per-project
registration, never on a shared or Desktop one.

A filesystem root, the home directory, or a system or program directory is refused
with `workspace_unsafe`: on Windows the Windows directory, `%ProgramFiles%`,
`%ProgramFiles(x86)%`, `%ProgramData%` and `C:\Users` itself; on POSIX `/usr`,
`/var`, `/home`, `/Users` and similar, plus `/etc`, `/bin`, `/usr/bin` and the other
system subtrees (`/usr/local`, `/private/tmp` and macOS temporary directories stay
allowed). Automatic launch-directory inference also refuses the Agents-Core
installation and runtime source tree, application bundles and editor extension
runtimes. If cwd is unavailable, resolution reports `workspace_required` instead
of selecting the installation. A missing or invalid directory is refused; memory
never silently falls back to a project in the installation.

One stdio process can serve several sessions. The Claude desktop app starts the
servers in `claude_desktop_config.json` once, outside a project, and can report
folders from every open session through `roots/list`. A single current root is
sufficient; multiple roots are ambiguous until the caller names a verified
`workspace`. Setup and `migrate` deny the Desktop entry in Claude Code (see the MCP client configuration),
so Code-tab sessions use their own server. These rules depend on signals supplied
by the native client; they do not establish that every client UI supplies them.

`log_interaction` and `read_history` results carry `workspace` (`root`, `source`) and
`pid`, and, after a failed history write, `history_last_error`
(`code=history_unwritable`, `errno`, `path`, `at`), which the model should mention once.
Over stdio their workspace errors, and those of `describe_repo`, `write_repo_summary` and
the flow tools, carry `pid` and `workspace_inputs` (`cwd`,
`claude_project_dir`, `agents_client_repo_root`, `client`, `roots`), and, in a server the
Claude desktop app started (client `local-agent-mode-<server>`), a `hint` that names its
`claude_desktop_config.json` and how its Code tab gets its own server; when the client
declares roots without a unique root and `workspace` was omitted, the refusal
asks for it and `log_interaction` tells the model to retry once with it.
Over HTTP, memory requires the registered `X-Agents-Workspace` header;
the daemon does not infer a project from its working directory. The user-scope
entries of Claude Code, Codex and Cursor are a bridge per session that registers the
session's project and sends that header itself; a fixed registered HTTP identity
is never replaced by an unverified tool argument ([details](shared-mcp-daemon.md#installation-and-client-migration)).
When no project resolves, `log_interaction` keeps the turn in the installation's
fallback journal (`unresolved/history.md` next to user sync's private state) with the
directory the client named as unverified metadata, and `read_history` reads the
entries kept for that directory; nothing is written to the named directory.
They move into a project only on request: `python -m src.daemon history adopt
--to /absolute/project --claimed <dir>`, or "Move to a project" on the History tab;
with user library sync they are shared like a repository's history
([details](shared-mcp-daemon.md#memory-and-errors)). `describe_repo`,
`write_repo_summary` and `repo:` flows still return `workspace_required`,
`workspace_unsafe` or `workspace_invalid`, with a `remedy`; routing remains
available. For HTTP without that bridge, register the project with
`migrate --workspace /absolute/project` (see
[service operations](shared-mcp-daemon.md#installation-and-client-migration))
and reconnect MCP; for stdio, set `AGENTS_CLIENT_REPO_ROOT` as described above.
Do not retry logging in a loop.

Source history and `CLAUDE.md` stay in the project. The summary hash is stored in
the project's `data/memory/`. Derived router and history indexes live in private
daemon state or, for standalone processes, in leased `data/stdio/` slots of the
installation (a temporary per-process directory on a file system without
byte-range locks). These indexes can be rebuilt without deleting the source
history.

See [service memory behavior](shared-mcp-daemon.md#memory-and-errors) for the
current transport contract and [the original memory design](memory-subsystem-spec.md)
for its rationale.

`log_interaction` stores the supplied prompt and response by default, after the
caller has replaced secrets and capability values with safe references and left
out prohibited personal details; callers can provide curated `intent`, `action`,
and `outcome` fields. This checkout ignores
`history.md` and `history/`. In a client project, check the ignore rules for
`history.md`, `history/`, `data/memory/` and the hidden lock files
(`.history.md.lock`, `.CLAUDE.md.lock`, `.agents-description.lock`) before
committing its action log.
