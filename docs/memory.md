# Repository memory

Source of truth for behavior: `src/memory/`, `src/memory/describer.py`,
`src/memory/config.py`, `src/server.py`.

This file describes how the server stores a repository summary and an action
history for each client project, and how it selects that project.

The server stores repository summaries and action history separately for each
client project.

Summary destination checks use filesystem identity as well as resolved paths,
including case variants and macOS volume aliases of the protected checkout.

- **`describe_repo`** — reads repository context without sampling or writing project files, including with `force_refresh=True`. Returns `not_a_repository` with an `instruction` to continue without a description for a plain folder, such as a task or download folder: one with no `.git` file or directory at or above `repo_path` below the home directory (the check is for existence, so a linked worktree's `.git` file counts) and no `CLAUDE.md` or `AGENTS.md` of its own; the home directory itself never qualifies, `up-to-date` with the cached summary preview, or `needs_summary` with a prompt, `repo_hash`, `repo_path`, `workspace_id` and an `instruction` for the calling model. The prompt's context bundle (directory tree to depth 3, root manifests, first 200 lines of `README.md`) is capped at 24,000 shown units, the size Claude Code counts for a tool result, so the whole response stays under its 50,000-unit inline limit and is shown inline; a part over its cap is cut at a line boundary and ends with a note of what it left out. A refresh is needed when the repository fingerprint changes (top-level names, first- and second-level directory names, key manifest contents and the head of `README.md`), the managed section is missing, or `force_refresh=True`. Both stdio and HTTP use the same read-only behavior.
- **`write_repo_summary`** — explicitly persists the caller's overview in the managed *Repository Memory* section of `CLAUDE.md` and updates its fingerprint cache. Follow the description's `instruction` before calling it. For such a plain folder it answers an `error` with `code` `not_a_repository` and writes nothing. The running Agents-Core installation and runtime source root are protected: writes return `rejected` with a `live_installation` reason before creating a lock, summary or cache. Existing summaries remain readable there; keep a new overview in the answer or use a separate worktree and describe that target again. A worktree's default memory root can normalize to the main checkout. A nested worktree inside the authorized workspace can be selected with its explicit physical `repo_path`; for an external worktree, first set a per-project `AGENTS_CLIENT_REPO_ROOT` to its physical path and reconnect MCP, or register that physical root over HTTP. Ordinary project and explicitly selected worktree summaries retain their existing storage and freshness behavior.
- **`log_interaction`** — end-of-turn logger. Commits `intent / action / outcome` and optional files, tags and persona attribution to the project journal before returning `history.state: durably_accepted`. Legacy calls retain content-hash deduplication and fallback-journal behavior. Pass an optional `idempotency_key` for durable retry protection: the same payload returns the original memory receipt and answer timestamp; a changed payload is rejected. Keyed calls require a trusted workspace. It returns a local `timestamp` (`YYYY.MM.DD HH:MM:SS`), which the final answer shows as its first line when the footer lists the `answer-timestamp` rule. A partial, malformed or mismatching `persona`, or `persona_action` without `persona`, never drops the turn: the entry is written with an `unverified`/`mismatch` persona line and the response carries `warnings`; a turn whose project does not resolve goes to the installation's fallback journal when the call carries no `idempotency_key`, while a keyed call without a trusted workspace is rejected and not saved there. An optional `record` adds a closed, typed map of the turn's numbers under `metadata.record`; an invalid one is refused before anything is written (see [turn records](turn-records.md)). A failed history write is reported as `history_last_error`. Langfuse remains best effort; semantic indexing and Personal Git sync run separately. The journal format, monthly archives and sharing approvals remain compatible. See [memory acceptance and retry guarantees](memory-acceptance.md) for failure states, retention and storage requirements.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing. A listing stays under the 50,000 characters above which Claude Code saves a tool result to a file. `outcome` comes as a preview of up to 600 characters, `intent` and `action` of up to 300, `files` and `tags` keep up to 20 items, and a shortened entry gives each cut field's full length in `truncated` (characters, or items for a list). When that is still too much, the previews get shorter, and then the last entries are left out, counted in `omitted`; `offset` skips that many entries, and the `instruction` gives the `offset` and `limit` that read them; entries logged between two calls shift those pages. Recent entries come from `history.md`, a pending rotation and the `history/YYYY-MM.md` archives, newest month first, with or without library sync, and `tag` keeps those carrying exactly that tag (`#` optional), in both modes. Semantic results carry `intent`, `action` and `outcome` like recent ones, without `files` and `metadata`, so a retrospective reads [turn records](turn-records.md) in recency or entry mode. `read_history(entry_id=...)` returns one entry whole, also one that rotation moved to `history/YYYY-MM.md`. With [user library sync](user-sync.md#repository-history) set up, a repository's top-level checkout (with an `origin`) reads its `history.md` and `history/*.md` merged with the entries the user's machines shared for that repository, including this machine's other checkouts: one per entry hash, the checkout's own copy first, ordered by time and read newest month first. Each entry carries `machine` (null for the checkout's own entries, else the label of the machine that wrote it); `machine=<label>` keeps that machine's entries and `machine=local` this checkout's own. `history.md` itself stays the checkout's journal. A machine shares a repository's entries only after its owner reviewed them with `python -m src.user_sync history export`: approval covers the repository's later entries on that machine and exactly the past entries listed, so another checkout that joins later waits for its own review. `python -m src.user_sync history share all` shares every repository's entries from that machine without a review instead, except repositories `history revoke` leaves out.
- **`claim_lookup`** and **`claim_record`** — the validated-claims store: verdicts about factual claims in `data/claims/claims.jsonl`, filtered into a confidence that goes stale and flags contradictions. See [validated claims](validated-claims.md).
- **`read_shared_memory`** — reads what the AI clients keep in their own memory files on this machine (Claude Code's per-project memory, Codex's memories), read-only, and returns recent facts, a lexical and semantic search over them or one fact with as much of its text as fits, each with its sources. Files with a detected credential are skipped and counted (detection is by pattern, so an unusual form can pass), other projects' facts show only under `scope=all`, and the semantic index builds in the background. See [shared memory](shared-memory.md).

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
When no project resolves, an unkeyed `log_interaction` keeps the turn in the installation's
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

## Turn logging by a Stop hook

A Claude Code session can leave logging to its client: a headless run (#734) and an
interactive session (#701). Through `log_interaction` the model writes its whole answer once
more as `response_content`, one more model call re-reads the whole context after the tool
returns, and nothing checks that the answer the user then reads is the one logged: a reworded or
translated delivery, or a note the model adds after another Stop hook asks for one more step,
leaves the journal holding an answer the user never saw. When the Stop hook logs the turn, the
journal holds the last assistant text of the turn, the one the user saw.

Where the hook logs, the server instructions and the full `SUCCESS` instruction tell the model
to end its answers with the footer, add no time line and not call `log_interaction` (the
`finish` variants `hook`, `server-hook` and `bundle-hook` in
[finish.md](../src/engine/protocol/finish.md)); the pointer of a later `SUCCESS`, the section
pointer and the server pointer end the same way (`bundle-finish-hook` in
[pointer.md](../src/engine/protocol/pointer.md)). The managed section states the same rule for
a client that reads it: skip every `log_interaction` step and add no time line. The time line
needs the `timestamp` that only a `log_interaction` call returns, so the
[answer-timestamp](../rules/rule-answer-timestamp.mdc) rule omits it; the hook records the time
in the entry's `answer_timestamp` instead.

Two setups switch this on. A headless run sets `AGENTS_TURN_LOG_HOOK=1` and
`AGENTS_CLIENT_PROFILE=headless` in the environment of both the server and the hook, and the
server then leaves logging to the hook for every client it serves; either switch alone changes
nothing. An interactive Claude Code session registers the hook with `--interactive`. The server
decides per client, because one daemon serves several (`stop_hook_logs` in
[client_instructions.py](../src/client_instructions.py)): only a request that names
`X-Agents-Client: claude-code` and whose Claude Code settings register
`python -m src.memory.turn_log_hook --interactive` as a Stop hook gets the hook wording. The
server reads the user's `settings.json` (in `CLAUDE_CONFIG_DIR` of the server's environment, else
`~/.claude`) and `.claude/settings.json` and `.claude/settings.local.json` of the session's
checkouts; a registration in the user's file counts for every project, one in project settings
must be in every checkout the session may run in, and `disableAllHooks` in any of these files
keeps the model's logging step. Codex, Cursor, the Claude desktop chat, a stdio server without
the headless switches and a Claude Code session whose settings the server cannot see that
registration in keep `log_interaction`. Where the server cannot see a registration that Claude
Code does load (managed settings, `--settings`, a plugin's hooks), the model still logs, and the
hook leaves alone a turn whose `log_interaction` call was recorded, so no turn is logged twice.

The Claude Code Stop hook [`python -m src.memory.turn_log_hook`](../src/memory/turn_log_hook.py)
then writes the entry that `log_interaction` writes with its defaults, through the same journal
writer and its deduplication: the prompt that started the turn, the final answer without a time
line, and the persona of the last `SUCCESS` or `NO_CHANGE` in the transcript with the persona
action that follows from where it happened. It takes the final answer from the hook input's
`last_assistant_message` when Claude Code sends one, else from the transcript, and resolves the
project as a stdio server does: `AGENTS_CLIENT_REPO_ROOT`, then `CLAUDE_PROJECT_DIR`, then the
session's `cwd`. It makes no model call, sends no Langfuse trace and uses no network. A turn is
logged once: the entry carries the hook input's `prompt_id`, a later Stop event of the same
turn (the re-entry after another Stop hook blocked it, `stop_hook_active`) finds it and writes
nothing, and a re-entry without a `prompt_id` writes nothing either. A turn the user did not
type, opened by a task notification, a message from another session, a scheduled task or a
host event (`turnOrigin` other than `human`), is its own turn and is logged once like any
other; Stop hook feedback stays inside the turn it continues. A turn without an applied persona
is still logged, as `log_interaction` logs a call without a descriptor: attributed to the agent
of the answer's footer, or to `unattributed`, and marked `Persona (unverified)`. Secrets are masked by format (GitHub, Anthropic, OpenRouter, OpenAI, Slack and Google keys,
AWS key ids and secret keys, JWTs, private keys and Bearer tokens) and by key name (`capability`,
`api_key`, `secret`, `token`, `password`), but no model reads the text first, so other details of a
prompt or an answer stay as written. A persona that `run_flow` activated attributes the turn too. Without a switch, a prompt, an
answer or a safe project it writes nothing; it always exits 0 and prints nothing, so
a failure never blocks the client.

To enable it for a headless installation, register the hook in Claude Code's managed settings
(`managed-settings.json` in `/Library/Application Support/ClaudeCode` on macOS or
`/etc/claude-code` on Linux and WSL, see
[managed settings](https://code.claude.com/docs/en/managed-settings)), with the installation
path in place of the placeholder (the one-command installer uses `~/.agents-core`):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd /absolute/path/to/Agents && .venv/bin/python -m src.memory.turn_log_hook",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Then start each headless run with both switches in its environment and a standalone stdio
server entry, for example `AGENTS_CLIENT_PROFILE=headless AGENTS_TURN_LOG_HOOK=1 claude -p
"<task>"`. Claude Code starts its hook commands and its stdio servers with its own environment,
so the hook and the server read the same switches; a switch set only in the server entry's
`env` does not reach the hook. Interactive sessions on the same machine start without them: they
keep logging through `log_interaction`, and the hook writes nothing for them.

To enable it for interactive Claude Code sessions, register the same hook with `--interactive`
in the user's settings (`~/.claude/settings.json`) or in a project's `.claude/settings.json` or
`.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd /absolute/path/to/Agents && .venv/bin/python -m src.memory.turn_log_hook --interactive",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

No environment switch is needed, and the server needs no restart: it reads the registration at
each initialize and each activation. A session started before the registration keeps the
instructions it was initialized with until its next activation, and the hook leaves the turns it
logs through `log_interaction` alone. Remove the registration to return to model-side logging.
