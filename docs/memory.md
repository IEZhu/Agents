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
`history.md`, `history/`, `data/memory/`, the hidden lock files
(`.history.md.lock`, `.CLAUDE.md.lock`, `.agents-description.lock`) and the
temporary file a write killed before its move can leave beside the journal
(`.history.md.<16 hex digits>.tmp`, which the next write removes) before
committing its action log.

## Turn logging by a Stop hook

A Claude Code session can leave logging to its client: a headless run (#734) and an
interactive session (#701). Through `log_interaction` the model writes its whole answer once
more as `response_content`, one more model call re-reads the whole context after the tool
returns, and nothing checks that the answer the user then reads is the one logged: a reworded or
translated delivery, or a corrected answer after another Stop hook asked for one more step,
leaves the journal holding an answer the user never saw. When the Stop hook
[`python -m src.memory.turn_log_hook`](../src/memory/turn_log_hook.py) logs the turn, the
journal holds the last answer the turn delivered.

**Who decides.** The session itself, never a settings file the server can read, because a hook
that does not run cannot announce anything. An interactive session registers the hook with
`--interactive` for both `UserPromptSubmit` and `Stop`. Before each prompt reaches the model, the
hook puts the Agents-Core turn-log notice beside it (`turn_log_notice` in
[protocol_text.py](../src/engine/protocol_text.py), composed from the `finish` variants `notice`,
`hook-calls` and `notice-close`): this session's Stop hook records the turn, so the model makes no
per-turn `log_interaction` call and adds no time line. Every surface that tells the model to log
yields to that notice for its turn: the server instructions and the full `SUCCESS` instruction
(`finish:notice-rule`), both pointers and the `NO_CHANGE` instruction, the managed section and the
memory reminder. A session that does not run the hook gets no notice and logs through
`log_interaction` as before: another `CLAUDE_CONFIG_DIR` than the one that holds the
registration, `claude --bare`, `claude --restricted`, `--setting-sources` without the source that
holds it, `disableAllHooks`, `allowManagedHooksOnly`, or a folder whose workspace trust was not
accepted. So does a session whose project does not resolve: the hook gives no notice there, and
the model's call keeps the turn in the fallback journal. Codex, Cursor and the Claude desktop chat
never see a notice.

A headless run sets `AGENTS_TURN_LOG_HOOK=1` and `AGENTS_CLIENT_PROFILE=headless` in the
environment of both the server and the hook, and the server then tells every client it serves
that the Stop hook logs (the `finish` variants `hook`, `hook-calls`, `server-hook` and
`bundle-hook` in [finish.md](../src/engine/protocol/finish.md), `bundle-finish-hook` in
[pointer.md](../src/engine/protocol/pointer.md)); either switch alone changes nothing. When the
two sources differ, the notice decides its own turn and the latest `SUCCESS` or `NO_CHANGE`
decides over the server instructions; a `NO_CHANGE` carries the current decision as well.

**What the model still does.** It ends every final answer with the footer. It still calls
`log_interaction` when a flow or the user asks for one, as `thread-close` does for its record and
tags and `autodev-retro` for its tag, and when the prompt or the answer holds personal details or
a secret the hook's detectors may not recognise: then it logs the turn redacted itself, or passes
`journal=false` to keep the turn out of the journal, with placeholders, not the turn's text, as
`query` and `response_content`. The hook then leaves that turn to the call, and the timestamp the
call returns gives the time line only when the footer lists `answer-timestamp`, as the
[answer-timestamp](../rules/rule-answer-timestamp.mdc) rule says. Before a closing call after which
no text is shown, such as the next-step box of the Claude desktop app (`wrap_up`), it gives the
final answer as text: the hook logs the last text of the turn and never the box. A Stop hook of your own that demands a `log_interaction` call at the
end of a turn has to be switched off together with this registration: otherwise every turn it
blocks is logged by the model, so the journal keeps the model's text again, and the hook removes
its own entry of the turn in favour of that call.

**The entry.** It is the one `log_interaction` writes with its defaults, through the same journal
writer: the prompt that opened the turn as intent (`[image]` or `[no text]` for a prompt without
text), `Agent: <agent>` and the persona attribution line as action, and the final answer without
a time line as outcome, taken from the hook input's `last_assistant_message`, else from the
transcript (`[no text answer]` for a turn that delivered none). The persona is the descriptor of
the last `SUCCESS` or `NO_CHANGE` in the transcript, a `run_flow` activation included, of a server
registered as `Agents-Core` or `Agents-Core-Desktop` in any letter case, also as a plugin's server
(`plugin_<plugin>_Agents-Core`); another server's tool of the same name does not attribute the
turn. The model's own `log_interaction` call counts under any server name (`Agents_Core`, as the
evaluation runner registers it, or a name of your own), so a turn it logged, or kept out with
`journal=false`, is never written by the hook as well; under a name that is no Agents-Core
registration a call counts only with Agents-Core's arguments (`agent_name`, `query` and
`response_content`, or `journal`), so another server's tool of that name, such as a CRM's
`log_interaction`, leaves the turn to the hook and withdraws no entry. The agent
is the one the delivered footer names, and the descriptor is reported against it: `mismatch`
when the footer names another agent, `unverified` with `missing=footer` when the answer has no
footer, `unverified` and the footer's agent without a descriptor, and `unattributed` without
either. A `SUCCESS` in the turn decides its persona action, whatever `NO_CHANGE` follows it. The
metadata holds `logged_by: stop_hook`, `client`, `session_id`, `prompt_id` and the time of the
Stop as `answer_timestamp`. A turn the user did not type, opened by a task notification, a
message from another session, a scheduled task or a host event, is its own turn; Stop hook
feedback stays inside the turn it continues, and a prompt the user typed is a prompt even when it
begins like such a row.

**One entry per turn, the one delivered last.** The entry is written under an idempotency key made
of the session's and the prompt's ids. A later Stop of the same turn, after another Stop hook
continued it, replaces the entry under the journal lock, wherever rotation has moved it, so a
corrected re-delivery is what the journal keeps; the same text again writes nothing. A later text
under a quarter of the entry's answer, footers aside, is a note beside it, such as "Chapter
marked.", and leaves the entry as it is. Two sessions never share a key. A turn whose
`log_interaction` call recorded it (`recorded`, `duplicate`, or `queued` from an older daemon), may
have recorded it (an `unknown` state such as `history_timeout`, whose write may still commit), or
whose call has no result in the transcript after the hook read it again for two seconds, is the
model's: the hook removes its own earlier entry of the turn, keeping its receipt so that no later
Stop writes it again, and writes nothing. A call with `journal=false` does the same and keeps the
turn out altogether. A turn whose own call the server refused (an error result, or a history
status other than those) is not logged at all, as it was not before this hook: the hook removes
its own entry of the turn, writes nothing, since the call may have been redacting what the
transcript holds, and exits 1 with the call's error code. A `journal=false` call answered with
a written status (`recorded`, `duplicate`, `queued` or an `unknown` state) came from a server
older than that parameter, which wrote the turn: the hook removes its own entry and exits 1,
naming the status; one the server refused kept nothing
either way, and the hook reports it the same way. A Stop input without a `prompt_id`,
from a Claude Code version before it, ties its `last_assistant_message` to the transcript's last
turn only once that turn holds the message, and is a reported failure when the transcript still
lags after the wait, so it never pairs the answer with the previous prompt and replaces that turn's
entry. When rotation has moved the entry into an archive, its replacement is written into
`history.md` first and the archived copy removed after it, so a hook killed in between leaves the
new entry and an older copy, which readers rank below it and the next write under the key removes.
With [user sync](user-sync.md#repository-history) set up, an entry the hook replaces or removes is
recorded as deleted, before the journal changes, for the next reconcile of a process that shares
this repository's history, which takes it out of what this machine shared and shares the
replacement in the same run; merged reads leave it out at once. A journal change that fails after
the record, or an interruption inside the record step, takes the record back for what the journal
still holds, and a file that cannot be read back then holds the entry only while it is the file the
entry was read from; a hook killed inside the change itself (about a millisecond) leaves the
record, so other machines read no entry of that turn until its next Stop, as
[user sync](user-sync.md#repository-history) says. A replacement carries a later time
than the entry it replaces, one second later when both fall in the same second, since it keeps that
entry's id.

**Secrets and private details.** The owner decided on 2026-11-27 that the hook writes by default
and that secrets are cleaned, not written, and on 2026-11-28 that a line naming a secret loses the
rest of the line rather than one more pattern per syntax, accepting that a harmless line tail is
sometimes lost from the journal. Before anything is written, the hook masks
([turn_redaction.py](../src/memory/turn_redaction.py)):

- **the rest of every line after a word that names a secret**, separators and symbols included,
  whatever the value looks like, punctuation alone after a separator too. The words are
  `password`, `passwd`, `passphrase`, `secret`, `token`, `credential(s)`, `creds`, `apikey`, `pswd`,
  `assword` (an expect script's prompt), `kennwort`, `contraseña` and the pairs `api key`, `access
  key`, `private key`, `signing key`, `encryption key`, `master key`, `seed phrase`, `recovery
  phrase` and `identified by`, any word ending in `password`, `passwd`, `passphrase`, `secret` or
  `token` (`PGPASSWORD`, `clientSecret`), a word of one case that begins with one of those and goes
  on with letters (`tokenstr`, `secretval`, `PASSWORDVALUE`; not `tokenize`, `secretary`,
  `passwordless` or a property such as `tokentype`), a misspelling of `password` one letter away
  (`pasword`, `passowrd`, `pssword`, `passwort`), a word that mixes Cyrillic letters into Latin ones
  (`pаssword`), and the Russian `пароль`, `токен`, `секрет` in every form, `пассворд`, `креды`,
  `пороль` and the pairs `код доступа`, `пин-код`, `сид-фраза`, `кодовое слово`, `кодовая фраза`
  and `секретное слово` in their cases; in any case, in camelCase, snake_case, kebab-case or dotted
  names, after `$`, `@`, `%`, `-`, `--` or `-D`, in quotes or escaped quotes, in XML elements,
  subscripts and calls (`DB_PASSWORD=`, `$authToken =`, `-Dapi.key=`, `config["password"] =`,
  `define('DB_PASSWORD',`, `<password>`, `\"token\":`, `Пароль от базы:`), and as the start of a
  value glued by a hyphen or a dot (`token-V`). Words common in prose name a secret in such a name
  or form, before `=`, `:`, `->`, a quoted value or a pair's quoted value, as a table cell before a
  value, before a call in a dotted name ending in one (`req.SetBasicAuth(`, `crypto.createHmac(`,
  `hmac.New(`), and in a sentence when a token shaped like a value (a digit, a symbol, a random run)
  follows within three words with no sentence ending between ("the key is V", "pwd V", "ключ от
  апи: V", "* pass — V"): `key`, `keys`, `pass`, `pwd`, `pw` (also `p/w`), `psw`, `passw`,
  `passcode`, `psk`, `auth`, `authtok`, `authorization`, `cookie`, `bearer`, `oauth`, `otp`, `hmac`,
  `dsn`, `mnemonic`, `parol`, `ключ`, `пасс`, `кей`, the plurals `passwords`, `secrets`, `tokens` and
  `passphrases`, `session id`, `connection string`, a `code` or `codes` after `recovery`, `backup`,
  `access`, `activation`, `verification`, `door`, `gate`, `alarm` or `unlock`, and `security
  answer`, and any word of one case glued to `pass`, `passwd`, `pw`, `pwd`, `psk`, `key`, `auth`,
  `authtok`, `secret`, `token` or `sessionid` (`dbpass`, `rootpw`, `DBPWD`, `masterauth`,
  `requirepass`, `privkey`, `JSESSIONID`; not `bypass`, `compass` or `monkey`), and a last `Pw`
  after a case change (`adminPw`, `dbPW`). `pin`, `пин`, `код` and `salt` count before `=`, `:` or a
  quoted value and before a value in a sentence ("the PIN is 4821"); `seed`, `session`, `sig`,
  `signature` and `private` only as the last word before `=`; `login(`, `signin(` and
  `authenticate(` before their arguments. A last `name`, `path`, `file`, `type`, `mode` or a time
  word makes a name a property; `id`, `header(s)`, `command`, `cmd` and `param(s)` do only when the
  value is a path, a number or one plain word (`VAULT_SECRET_ID=<uuid>` and `AUTH_HEADER="Basic …"`
  are masked, `api_key_header = "X-API-Key"` stays). The password options of clients count the same
  way: a password option anywhere on a line with a user option, attached to its value or not and
  never before a port (`-p`, `-P`, `-w`, `/p:`, `/rp` with `-u`, `-U`, `--user`, `/u:` or `/ru`, a
  quoted user too: `podman login -u U -p V`, `cqlsh -p V -u U`, `mycli -uroot -pV`, `ipmitool -U
  "U" -P "V"`, `xfreerdp /u:U /p:V`, `schtasks /ru U /rp V`; not `docker run -u 1000 -p 8080:80`),
  the value after a user option's user (`lftp -u U,V`, `etcdctl --user U:V`, `rpcclient -U U%V`;
  not a `uid:gid` pair), and the options of known clients without a user option beside them:
  `mysql -p` and its siblings, `docker login -p`, `sqlcmd -P`, `redis-cli -a`, `curl -u`, `--user`
  and `--proxy-user`, `sshpass -p`, `http -a`, `ldapsearch -w`, `smbclient -U`, `7z -p`, `zip -P`,
  `openssl -k`, `mongosh -p`, `ssh-keygen -N` and `-P`, `rar` and `unrar -p` and `-hp`, and what
  follows `::add-mask::`;
- **the value a label line leaves below it**: the lines indented under `password: |`, `secret:`,
  `- name: DB_PASSWORD`, `tokens {` or `variable "db_password" {`, without their fence lines;
  under a line that opens a block of code (`def check_password(user, password):`, `class
  TokenBucket:`, `if (!token) {`) only the block's quoted literals, and each of its lines is read
  again by the rule; below a bare label (`Password`, `Passwords`) or a line that ends in a colon after
  a word naming a secret ("The password for the server:", "Логин и пароль:"), the next non-blank
  line and those after it that read as a value (one token, a code span, a pair, or four tokens at
  most with one shaped like a value; a list item only when its text reads so, so the steps under
  "To rotate the token:" stay), up to a blank line or eight lines, and past the eighth line the rows
  of the same shape as the one above (a list's items, pairs), up to 1,000; below a sentence or a
  heading that ends in such a word without a colon ("Try logging in with the password", "### Admin
  passwords", "I reset the admin password."), the lines that read as a value; below a sentence whose
  few last words point there ("The new root password is below.", "Password (see below)") and below
  a label line that leaves its value without reading as a sentence (`printenv DB_PASSWORD`,
  `echo $API_TOKEN`, `op read op://Private/db/password`, `What's the wifi password?`), a lone value
  on the next line (one token shaped like a value, or a quoted string); after a line that starts
  with a shell's or a REPL's prompt and names a secret, anywhere on the line (`$ vault kv get
  -field=password secret/db`, `PS C:\> $env:DB_PASSWORD`, `>>> os.environ["DB_PASSWORD"]`,
  `127.0.0.1:6379> CONFIG GET requirepass`), the output lines that read as a value, up to a blank
  line, a fence, the next prompt or eight lines; a fenced block first below a label, whose content goes and whose fences
  stay (forty lines at most), and never a fence that closes a block; the next line after a quoted
  field name (`name="password"`); a heredoc's body after a line that names a secret (`chpasswd
  <<EOF`, fifty lines at most); the lines a masked line continues with trailing backslashes (fifty
  at most), and a client's option on such a continuation line (`mysql -u root \` then `-pV`); the
  value field of the same object or list item as a name field that names a secret, eight lines each
  way at most, in JSON, YAML or XML (`"key": "db_pass",` then `"value": "V"`, `<name>…Password</name>`
  then `<value>V</value>`, `- value: V` above `name: DB_PASSWORD`); after an element whose text names
  a secret, the element on the next line whatever its tag (`<key>Password</key>` then
  `<string>V</string>`, `<dt>`/`<dd>`, `<th>`/`<td>`); and the rows under a table's header that names
  a secret in one of its cells, up to 1,000, its rule and border lines kept: a pipe table with or
  without outer pipes, psql's rule of dashes joined by plus signs, a bordered MySQL or
  reStructuredText grid, comma, semicolon or tab separated values with quoted fields read as one
  and any text in a header cell (`"Smith, John"`, `created_at(UTC)`), columns set by blanks above a
  rule of dashes or equals signs (sqlite's column mode, a simple reStructuredText table), a header
  in capitals or title case above rows of as many columns (`NAME  PASSWORD`), and a comment of
  column names (`# client  server  secret`). After two blank lines, and up to eight, only a line
  that reads as a value is taken. A YAML or INI sibling key below an empty key (`password:` then
  `username: admin`; not `admin:V`, which is no key) is no value of it, a key block below a label is
  the key block rule's, and no fence line is ever masked;
- **a quoted value** that opens after a label's separator, a blank or a client's option (`"`, `'`,
  `` ` ``, escaped quotes, `"""` and `'''`), to the end of the line it closes on, within twelve
  lines; a quote of a kind that stands an odd number of times before the separator closes the
  label's own string or code span and opens nothing (``Set `password:` in``, `print("password:",
  pw)`), a single quote opens a value only with one after it on its line and is closed by none
  inside a word ("It's"), and without a close the label's line alone goes;
- **a value written before its label** (the owner's call on the final verification's ninth
  finding): one piped by `echo` or `printf` into, or given as a here-string (`<<< "V"`) to, a
  command that reads a password on its standard input (`--password-stdin`, `chpasswd`, `passwd
  --stdin`, `sudo -S`, `kinit`, `gpg --passphrase-fd`, `openssl … -pass stdin`, `wrangler secret
  put`, `gh secret set`, `ansible-vault encrypt_string --stdin-name`), a token shaped like a value
  right before `as`, `into` or `for` with a word naming a secret within four words ("use V as the
  password", "I typed V into the password field"), and one right before a bracketed word that
  names one ("admin / V (password)");
- token formats, keeping their prefix (`ghp_***`, `sk-proj-<secret>`): GitHub, GitLab, Anthropic,
  OpenAI, OpenRouter, Slack (`xoxb-`, `xoxp-`, `xapp-` and webhook URLs), Discord bot tokens and
  webhooks, Google, Stripe, SendGrid, Twilio `SK` keys, Telegram bot tokens, Hugging Face and npm
  tokens, DigitalOcean, Postman, New Relic and Airtable tokens, AWS `AKIA` and `ASIA` key ids, JWTs
  and Bearer values; the body of a private key block, PGP ones included, between its armour lines,
  line by line with every line break and indentation kept (the scanner then masks the BEGIN line,
  and the END line stays), a body whose BEGIN line is missing, and key material printed as hex pairs
  (`openssl -text`);
  credentials in URLs and DSNs (`redis://:PASSWORD@`, a password holding an at sign or a slash,
  `//user:PASSWORD@`, `user:PASSWORD@tcp(db)`), Oracle's `user/PASSWORD@` after `sqlplus`,
  `userid=`, `connect`, `rman`'s `target` and in a thin JDBC URL, and `sqlplus user/PASSWORD`, a
  token of 20 characters or more in a URL's user position (`https://<token>@github.com`), the
  values of the query parameters `code`, `sid`, `session_id`, `sessid`, `auth_code` and `ticket`,
  squid's `login=user:PASSWORD`, and pgpass lines;
- random-looking strings without a label: runs of 20 or more letters, digits, `_` and `-`, or of
  base64 with `+` or `/`, that look random rather than like a name, every 43-character run that is
  not a plain snake_case, SCREAMING_CASE or kebab-case name, hexadecimal runs of 32 digits or of
  a length from 33 on that no commit name or digest has, glued to a prefix or a suffix or not
  (`shpat_…`, `key-…`, `…-us21`), base32 secrets (a TOTP seed), 40-character AWS secret keys, two or
  more lines of base64 (each line masked on its own), and high-entropy strings;
- then, until a scan finds nothing, what the outgoing-text scanner of self-development finds
  ([autodev guards](autodev-guards.md): token formats and private key headers, also split over
  lines or encoded), every value of the hook's own environment whose variable's name names a secret
  (by the same words, plural and joined names included: `API_KEYS`, `SERVICE_TOKENS`, `MASTERKEY`,
  `SSHPASS`, `ADMIN_PW`; not an everyday word that ends like one, `COMPASS`, `NO_BYPASS`, `HOTKEY`; a
  last word such as `name`, `path`, `file`, `id`, `type` or `mode` makes it a property, while
  `AUTH_HEADER`, `TOKEN_STORE`, `SECRET_ENV`, `API_KEY_PARAM` and `INPUT_TOKEN` count, and a quantity
  before a plural, `MAX_TOKENS`, is a count; or whose value scans as a secret) as a known secret,
  raw, split, base64 or hex, and the credentials that [shared memory](shared-memory.md) and
  [user sync](user-sync.md) refuse to serve or publish, read with shared memory's patterns bounded
  for time (shared memory itself keeps its own). An environment value under eight characters, or
  one naming an existing path, is no known secret.

A word used as an ordinary word in a sentence keeps its line when the rule can tell that nothing
after it is a value: the word stands alone, not quoted and not joined to other characters, and the
rest of the line is two or more ordinary words with sentence punctuation only, the first in lower
case, none of them a verb that introduces a value (`is`, `was`, `are`, `equals`, `set`, `это`,
`to` after `set`, `change` or `reset`, and `be` unless a modal or an auxiliary verb stands before
it), a chain of hyphenated words or a run of letters no word is made of (five consonants in a row,
or a pair such as `qx`): "the key point is", "auth flow works", "Reset your password from the
account settings page." A strong word right before a property word names that property, as a
name's last word does ("The password policy changed last week", "the token type"). After `seed
phrase`, `recovery phrase`, `mnemonic`, `passphrase`, `сид-фраза`, `кодовое слово` and `кодовая
фраза` no sentence exempts the line, since their values are ordinary words: "The seed phrase must
be written down on paper." loses its tail. In doubt the line is masked. The rule reads a turn's
lines once, from where the token formats and key blocks left them, and tells its masks from the
turn's own text by their offsets, so an element named `secret` is a label like any other. Every
rule works line by line or on a bounded window: a match that masks to its line's end is read once
per line, lookaheads and quoted values are bounded, no pattern restarts inside a run, a line's
tokens are read once for all its words, a name once for all its labels, and a window of continued
lines once. This is measured, not proven: of the attempt 4 verification's 63 hostile and large
inputs of up to 1 MB, the ten that took 5 to over 120 s take 1.1 to 4.1 s in five runs on the
machine they were measured on, beside other sessions' work, and the longest of all take 4.2 to
4.4 s; the attempt 5 verification's slow shapes, a hyphen-joined name of `key-` repeated (over 150 s
at 1 MB), a dotted name after a word (6.7 s), continued client lines (12 s) and lines piped into a
login (5.2 s), take 2.4 to 3.1 s at 1 MB, and the piped lines 4.6 to 4.7 s, most of it the
repository scanner's two passes, beside other sessions' work (load averages of 7 to 9); four times
the text takes about four times as long. Of that verification's 46 hostile inputs of 1 MB none
takes over 4.4 s, and of attempt 5's 63 none over 4.0 s.

A text the scanner cannot clean, too large, with too many hits, or not in the time left before the
hook's deadline, is not written: the field holds a placeholder and the entry's metadata
`redaction` names it.

Some text that is no secret is masked: the measures below count lines, and a paragraph of an
answer is one line, so a word of the rule early in it takes the rest of the paragraph when a digit,
a symbol, a code span, a quoted word or a verb such as `is` follows anywhere on the line. Per text
and per character, on the attempt 5 verification's independent benign corpus of 160 ordinary
answers and code (118 English, 42 Russian), before attempt 6 and after it: 116 texts change both
times; 11,776 of 30,463 characters go before (38.7%) and 10,368 after (34.0%); a changed text loses
92 characters at the median before and 90 after, half of its text before (50.6%) and 44.9% after;
60 changed texts lose more than half before, 52 after; 290 of 670 lines are touched before and 265
after, 96 masked whole before and 42 after, and 36 masked lines hold no word of the rule before and
19 after; by kind, prose 26 of 40 texts and 47% of its characters both times, code 28 of 30 and
45% before, 33% after, Markdown answers 15 of 15 and 42% before, 33% after, structured data 15 of 15
and 50% before, 48% after. As one text it keeps every line break and every fence line after
attempt 6 (828 of 830 lines and 29 of 30 fence lines before). Per line, as measured at attempt 5
of #701: of the final verification's 79 benign strings 40 change, mostly a line of
code or Markdown after a secret's name (`self.token = token`, `**Key:** Enter`, `Auth: OAuth 2.0`,
`def sign(key: KeyType, ...)`), header lines named for a key (`Idempotency-Key:`,
`Sec-WebSocket-Key:`), and ids that look random (an MD5, a dashless UUID, a KSUID, an ssh key's
body); of the attempt 4 verification's 128 benign lines 49 change (English prose 6 of
30, Russian prose 2 of 20, code 22 of 35, shell and config 10 of 23, docs and Markdown 9 of 20),
and as one text no line break is lost and 52 lines change; of 60 sentences of everyday engineering
prose 7 lose their tail, five a word that names a secret before `is` ("The token budget for this
model is generous.") and two after a seed phrase or a passphrase. Placeholders and expressions
after a label go too (`password=<your password>`, `api_key=os.environ["KEY"]`), and so do the lines
below a label line that read as a value. Anthropic API ids (`toolu_01…`, `msg_01…`, `req_01…`,
`session_01…`), GitHub node ids, a Google document's id in its link, subresource integrity
digests, the payload of a `data:` URI, a W3C `traceparent`, UUIDs, commit names, SHA-256 digests
and paths stay.

What reaches the journal, measured the same way: of the attempt 4 verification's fresh corpus of
305 secret-bearing items, 191 were masked, none in part and 114 written whole before attempt 5,
280, none and 25 after it, and 284, none and 21 after attempt 6, all 21 with nothing naming the value
before it (a positional argument); of a corpus of 220 secret-bearing texts written for attempt 5
before its rules changed, and never tuned against, 190, none and 30 before, 213, none and 7 after
attempts 5 and 6; of the attempt 5 verification's frozen corpus of 721 texts, the 864 secrets that
something names went from 833 masked, none in part and 31 whole to 858, none and 6, and its 117
boundary probes from 50, none and 109 to 134, one in part and 24 (the part four characters that the
label's own `wallet` shares with the masked phrase's `walnut`), each of the 24 a shape listed below; the final verification's 141 shapes stay at 140 masked (the Ethereum key below passes). All
are adversarial, so their shares are no field rates.

Deterministic redaction cannot catch everything, and these shapes still pass:

- free-form personal details, such as a name, an address, a diagnosis or an account written out
  in prose: they are not detected at all;
- a secret with no word that names one on its line, in a short or plain shape: a short or
  dictionary-word password in prose, a value only its position names, passed to a call
  (`connect("db", "admin", "...")`, `jwt.encode(payload, "...")`) or a command (`rabbitmqctl add_user
  app ...`, `net use Z: \\host\share /user:svc ...`, `curl -X PUT -d ... …/kv/app/db_password`), a
  value written before its label in other words than the forms above ("hunter2 is my password",
  "admin / V — password for the test box"), and a value after a sentence that names the secret
  without pointing below, or with a paragraph between it and the value's fence;
- a value after a prose-common word that a clause or more than three words stand between ("I
  rotated the key; the new one is V", "pw for the router: V", "auth for the internal dashboard V"),
  or after one ending a dotted name with nothing but a blank before the value (`nmcli connection
  modify office wifi-sec.psk V`), a field named by a single letter (`WIFI:T:WPA;S:Office;P:V;;`) or
  an option outside both lists (`psql -W V`, `hydra -p V`, `vsce publish -p V`), and words outside
  the lists (`serial:`, German, Spanish and French words other than `kennwort` and `contraseña`);
- a value of plain lowercase words, or of letters that read like a word, after a word that names a
  secret, inside a sentence of ordinary words with no verb that introduces it ("the token sunshine
  expires tonight", "пароль от вайфая sunshine"; a random run of ten letters passes like this about
  one time in four);
- a name of one case that begins with a weak word (`keystring = "V"`), and a property name that
  holds a plain word, a number or a cleartext value (`X_AUTH_HEADER=` with letters only,
  `secret_id:` with digits, `ADMIN_PASSWORD_HASH=` with a password);
- what lies past a bound: a fence's content past its fortieth line, a heredoc's past its fiftieth,
  a table's rows past its 1,000th, a quoted value that closes more than twelve lines on, a value
  more than eight blank lines below its label, a value field more than eight lines from its name
  in an object, a table whose header a title line or a blank line parts from its rows, and a pipe
  table headed by `Key`, which heads key-value tables;
- hexadecimal without a label of 40, 56, 64, 96 or 128 digits, which is the form of a commit name
  or a digest (a 64-digit Ethereum key after `0x` has a transaction id's form), and of fewer than
  32 digits;
- a random string without a label that is short or looks like a name: under 20 characters, or,
  rarely, a random one of 20 to 32 characters (about 4 in 100 at 20 to 24 characters and 1 to 3 in
  100 at 32, in 400 samples of each kind);
- a secret of the hook's environment under eight characters, or in a form the scanner does not
  decode: split into concatenated or quoted pieces, reversed, upper-cased, rot13, or only part of it
  (a 15-character prefix of 30, all but the last four characters). Environment names that end in
  `KEY` count whatever they name (`PUBLIC_KEY`, `PRIMARY_KEY`, `CACHE_KEY`), so their values of
  eight characters or more are masked wherever a turn holds them.

A turn that holds such details is protected by the model's own call: a `log_interaction` call with
the turn redacted, which the hook leaves alone, or `journal=false`, which keeps the turn out. The
journal is a file in the project that may be committed; check its ignore rules as above.

**Failures.** The hook makes no model call and uses no network. A Stop event it could not log exits
1, which Claude Code shows as a non-blocking hook error with the reason, such as
`turn not logged (journal_busy)`; it never exits 2, which would keep the turn going. A Stop event
has 25 seconds in all, for the transcript wait, the redaction and the journal lock (ten seconds at
most), and the command ends itself with exit 1 at 28 seconds, or at 29 seconds with a traceback
when one long computation holds the interpreter lock (Python's `faulthandler` watchdog, which
writes to the standard error, or to `os.devnull` kept open for the run when the process has none
with a file descriptor, so the bound holds then too), so the registration's `timeout` of 30 seconds
below is a minimum: with less, Claude Code may cancel the
hook and discard its output, and the turn is lost without a word. It reads the transcript line by
line, so memory follows the turn and not the file, up to 1 GiB, and a larger transcript is such a
failure too. A turn whose prompt row is still missing after two seconds of reading again is a
failure as well. A Stop event of a turn whose project does not
resolve writes to the installation's fallback journal that `log_interaction` uses, with the
session's directory as unverified metadata, and `read_history` reads it there.

**Limits.** A server entry that pins the workspace, through the `X-Agents-Workspace` header or
`AGENTS_CLIENT_REPO_ROOT` in the entry's `env`, does not pin the hook, which resolves the project
from its own environment: `AGENTS_CLIENT_REPO_ROOT`, then `CLAUDE_PROJECT_DIR`, then the session's
`cwd`. Set the same `AGENTS_CLIENT_REPO_ROOT` in the environment Claude Code starts hooks with, or
the two write to different journals. A turn interrupted with Esc has no Stop event and no entry,
as it had no logging call before, and neither has a turn that ends in an API error, which fires
`StopFailure` instead of `Stop`. Entries the hook writes, and the removals it records, reach
[user sync](user-sync.md#repository-history) with the next reconcile of a process that runs it,
such as the daemon's next append, a server start or a resume, not at once. The hook records a
removal in the sync state it resolves itself: `AGENTS_SERVICE_DIR` in its own environment, else
the directory the installed daemon recorded (`data/.shared-service.json`), else the default. A
server whose entry sets `AGENTS_SERVICE_DIR` only in its `env`, or another installation's
server, reads another state, and the removal stays in what this machine shared: set the same
`AGENTS_SERVICE_DIR` in the environment Claude Code starts hooks with.

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

To enable it for interactive Claude Code sessions, register the hook with `--interactive` for
both events in one settings file: the user's (`~/.claude/settings.json`, or `settings.json` in
`CLAUDE_CONFIG_DIR`) or a project's `.claude/settings.json` or `.claude/settings.local.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd /absolute/path/to/Agents && .venv/bin/python -m src.memory.turn_log_hook --interactive",
            "timeout": 30
          }
        ]
      }
    ],
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

Keep the two entries together: a `UserPromptSubmit` entry that runs where its `Stop` entry does
not would tell the model that a hook logs turns nobody logs. No environment switch is needed, but
every Agents-Core server process the sessions use, each stdio server and the daemon, must run this
version before `journal=false` is relied on: a server started before the update keeps its earlier
code until it restarts, ignores that argument and writes the turn, which the hook then reports.
Remove both entries to return to model-side logging: the next prompt carries no notice, and the
model logs that turn itself.
