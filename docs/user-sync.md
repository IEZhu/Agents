# User library sync

Status: the engine and its command line (#165), GitHub sign-in through the API (#166), scheduled
runs, the stdio trigger and the terminal wizard (#168), the sync loop of the macOS daemon (#167),
the status, Sync page and wizard of the web UI (#170) and the installers' step with setup from the
environment (#171) are implemented, as parts of the [epic #173](https://github.com/IEZhu/Agents/issues/173).

Sync keeps the personal library (`flows/.user`, or `AGENTS_USER_FLOWS_DIR`) the same on a user's
machines through one private git repository with one branch, which is never force-pushed. The
library becomes the working tree of a git repository at `flows/.user/.git`; nothing else in the
installation changes.

## Command line

Every runner uses `src/user_sync/` (standard library and the git CLI only). Where the macOS
daemon runs, it syncs by itself; use `python -m src.daemon user-sync …`, described in
[User library sync](shared-mcp-daemon.md#user-library-sync). Without the daemon:

```bash
python -m src.user_sync setup      # in a terminal: the wizard below walks through everything
python -m src.user_sync github login                          # GitHub: sign in with a device code
python -m src.user_sync setup --github me/agents-library --name "My Name" --email me@example.com
python -m src.user_sync setup --remote git@git.example.com:me/agents-library.git \
    --name "My Name" --email me@example.com [--label laptop] [--ask-new-repositories]
# another host: add the printed public key to the repository as a deploy key with write access
python -m src.user_sync check      # access, and the remote is empty or an Agents-Core library
python -m src.user_sync preview    # what would be uploaded and downloaded, and the conflicts
python -m src.user_sync start --confirm <hash from preview>
python -m src.user_sync run        # one cycle; --force ignores the retry delay
python -m src.user_sync status | conflicts | pause | resume | disconnect
python -m src.user_sync configure [--fetch-minutes 1-60] [--[no-]ask-new-repositories] \
    [--name "My Name"] [--email me@example.com] [--label laptop]
python -m src.user_sync resolve <conflict id> keep|mine|dismiss
python -m src.user_sync scope [--exclude GROUP] [--include GROUP] [--exclude-file PATH] \
    [--include-file PATH] [--allow-secret PATH] [--approve repos/<key>] [--confirm HASH]
python -m src.user_sync github login | status | logout | libraries | create [NAME] | add-key | regenerate-key
```

Each command takes `--json`; prompts and the sign-in code then go to stderr. `configure --name`,
`--email` and `--label` change the commit identity and this machine's label without the network;
a value that sync or git would refuse is refused before anything is saved (`identity`). `--state DIR` and
`--library DIR` before the command name the private state directory and the library explicitly;
scheduled runs pass both, so they never depend on the scheduler's environment. The command line reads the installation's `.env` like the MCP servers,
and holds the installation's shared session lease while it runs, so an update never replaces the
code under it. Setup records the library; a run on another library stops with
`library_mismatch`.

Hosts other than github.com print their host key fingerprints at setup; confirm one with
`--trust-host-key SHA256:…` after comparing it with the host's published fingerprint. github.com's
keys are refreshed from its API on every setup. Where privacy cannot be checked over HTTPS,
`--confirm-private` records the owner's confirmation. Setting up another remote starts a new
history there: one root commit with the current files, so old commits never travel to it.

### The wizard

`python -m src.user_sync setup` without `--remote` or `--github`, in a terminal, asks step by step:
GitHub sign-in with a device code (it opens the browser when it can) or an SSH URL for another
host; the repository (a new private one, a library found on the account, or another private
repository by `OWNER/NAME`); the commit name and email, which it never reads from your git
configuration; and the machine label. It then sets up sync (on GitHub it adds this machine's
deploy key; elsewhere it shows the public key and the host key fingerprints to confirm), checks
access, asks to confirm privacy where the host cannot be checked, and shows the preview: uploads,
downloads, conflicts, files the secret scanner held back and repository groups with their
origins. Sync starts only after you type `yes`. Without a terminal, `setup` prints its usage and
exits with code 2.

### The web UI

Where the macOS daemon runs, the settings page (`python -m src.daemon flows-ui`) shows the state
of sync in its header (`Synced 2m ago`, `3 pending`, `Offline, retry 14:05`, `Needs attention`,
`2 conflicts`, `Sync off` and so on) and opens the Sync page from there; see
[Sync page](shared-mcp-daemon.md#sync-page). While sync is off, the page walks through the same
steps as the terminal wizard: GitHub sign-in with the code shown on the page (or an SSH URL), the
repository, the identity and label, what syncs (everything on by default) and the preview, whose
hash Start sync sends. The browser never calls GitHub and never receives the token or the device
code; the daemon does the work, and operations that run git go through its sync task. Once sync
runs, the page shows the status with Sync now, Pause and Resume, the machines (the repository's
deploy keys on GitHub), conflicts with Open, Keep current, Use mine and Dismiss, what syncs with
the approvals waiting here, the GitHub account or the manual SSH key, the identity, the activity
of the last 20 cycles and Disconnect. A flow open in the editor that a cycle updates says from
which machine and offers Reload; unsaved text stays.

## GitHub

Setup does not need the `gh` CLI: Agents-Core signs in to the GitHub API itself
(`src/user_sync/github.py`) and uses it to create the repository, check its privacy and manage
deploy keys. Git never uses the GitHub token: it runs on this machine's deploy key.

- **Sign-in.** `github login` runs the OAuth device flow of the Agents-Core OAuth App: open
  `https://github.com/login/device`, enter the printed code, and approve the `repo` scope, which
  creating a private repository needs. The app's client ID ships in the code once the owner has
  registered the app; until then, and for forks or GitHub Enterprise Server, set
  `AGENTS_GITHUB_CLIENT_ID` (and `AGENTS_GITHUB_HOST`), for example in the installation's `.env`.
- **The token** is kept in the macOS Keychain, the Windows Credential Manager or the Secret Service
  (`secret-tool`), never in the library, logs or settings. Where none of them works, it goes to a
  private file in the state directory, and `github status` says so. `github logout` (Forget
  account) deletes it from this machine and prints the page where you revoke it on GitHub. A
  store that refuses (the Keychain in an SSH session) does not stop it when that store never held
  the token. When it may still hold an earlier sign-in's token, the account stays and the error
  says to run `github logout` where the store works (a desktop session) or to revoke the token on
  GitHub.
- **Repository.** `github libraries` lists your private repositories that hold an Agents-Core
  library; `github create [NAME]` creates an empty private repository (default `agents-library`).
  Before anything changes here or on GitHub, `setup --github OWNER/NAME` checks through the API
  that the repository exists, is private, is empty or holds a library on the sync branch, and
  belongs to you; a repository of an organization or another user needs `--confirm-owner OWNER`,
  because everyone who can read it there can read the library. It then adds this machine's public
  key as a deploy key with write access (titled `Agents-Core <label>`; a read-only one is
  replaced) and runs setup with the repository's SSH URL. GitHub accepts a key on one repository
  only: when an earlier setup that never started put it on another repository, `--move-key`
  removes it there. A repository that does not exist is refused with a hint to run
  `github create`. `github add-key` checks privacy and content the same way.
- **Privacy.** For a repository on the signed-in account's host, the check before the first upload
  and the daily check ask GitHub's API: public and internal repositories are refused. When the API
  cannot answer, the anonymous check decides: after a refused token (it was revoked; the account
  then shows "reconnect needed" in `github status`, and sync keeps running on the deploy key), an
  outage, a rate limit, a 403 or a 404. A privacy confirmation (`--confirm-private`) covers one
  repository: setup for another one clears it.
- **A refused key.** When `check` is refused on a repository of the signed-in account, its error
  says whether GitHub still has this machine's deploy key; `github add-key` adds it again. When
  `check` finds content that is not a library before sync started, it removes the deploy key
  `setup --github` added there, and says so.
- **A new key.** `github regenerate-key`, or Regenerate key on the Sync page, makes a new key
  pair. For a repository on the account's GitHub host the account must be signed in (otherwise
  `not_signed_in`, or `reconnect_needed`): the new public key becomes a deploy key first, then the
  key files are replaced under the sync lock, and after it the old deploy keys are removed: the one
  with the old public key and the one recorded as this machine's. When the new key cannot be
  installed, its deploy key is taken off GitHub again (the error names one that could not be),
  then the new pair is dropped; once the new private key is in place it is never rolled back, not
  even by an interrupt (Ctrl-C), which leaves the old deploy key on GitHub, listed under Machines
  on the Sync page with Remove. When its public key file cannot be written, the old public file is
  removed, so that ssh derives the public key from the new private key, and the error says so. For
  another host the new public key is shown to add by hand, and the remote refuses this machine
  until it is added.
- **This machine's deploy key** is recorded in the settings (`deploy_key_id`) whenever sync adds
  it or finds it on GitHub, so a new key and Disconnect on the Sync page remove it, and the Sync
  page marks it, also when its public key file is gone. `check` withdraws it from a repository
  that holds other content only when `setup --github` added it (`deploy_key_added`), never one it
  found. Setup derives a missing public key file from the private key instead of making a new key.
- **Signed out.** Sync itself never needs the account: it runs on the deploy key. Without it, the
  Sync page lists no machines, Disconnect cannot remove this machine's deploy key on GitHub (it
  says so), and a new key for a GitHub repository is refused.
- **Port 443.** When `check` cannot reach `git@github.com:…` on port 22 (some networks block it),
  it tries `ssh://git@ssh.github.com:443/OWNER/NAME.git`, GitHub's SSH service on port 443, and
  keeps that as the remote when it reaches GitHub there; a refusal on port 443 (a key or host key)
  is then the error `check` reports. Setup for the same repository keeps the port 443 URL and the
  sync history. Only `check` moves to port 443; an offline run on port 22 names it. The host keys
  for port 443 are written with github.com's at setup.

## Running without the daemon

```bash
python -m src.user_sync schedule enable [--interval MINUTES]   # default: the fetch interval, 5
python -m src.user_sync schedule disable | status
```

`schedule enable` installs one job per installation that runs
`python -m src.user_sync --state <dir> --library <dir> run` every few minutes for the current user:
a hidden Task Scheduler task on Windows (`pythonw.exe`, least privilege, only while the user is
logged on), a systemd user timer on Linux or one crontab line where `systemctl --user` cannot reach
a user manager, and a LaunchAgent on macOS unless the daemon is installed, which runs its own loop.
Enabling again replaces the interval; `disable` removes the job. Paths a scheduler would misread
are refused rather than quoted wrongly. A scheduler that fails or hangs is reported with the
reason `schedule`.

A stdio MCP server runs one cycle about ten seconds after a write to the library (a saved flow, a
persona or a switch) while sync is set up, started and not paused, and no other runner holds the
sync lock; a run that finds it busy is tried again a few seconds later. The command never blocks the request that saved.

## Installers

`scripts/init_repo.sh` and `scripts/init_repo.bat` run `python -m src.user_sync installer`
(`src/user_sync/installer.py`) after the client configuration, and `installer --summary` in their
summary. The summary says whether sync is on, whether it runs in the background (the daemon, or the
scheduled run) and how to turn either on. With a terminal and without `--yes`, the step asks once:
"Set up sync between your machines now? [y/N]". On macOS, yes opens the daemon's settings page
when the daemon answers `/health` as ready and serves the Sync page (its page calls
`/ui/api/sync`); otherwise it starts [the wizard](#the-wizard), without a connection error. The
one-use link is printed only when no browser opened it. After the wizard has started sync on a
machine without the daemon, it asks "Also sync every 5 minutes in the background? [Y/n]" and, by
default, runs `schedule enable`. It asks nothing under `--yes` or `AGENTS_ASSUME_YES=1`
(`install.sh` always passes `--yes`; on Windows `--yes` stops only this question), without a
terminal (on Windows, a console that answers `GetConsoleMode`: `isatty` is true for NUL too), when
sync is set up (it reports the state), when git 2.32 or newer or `ssh-keygen` is missing, or when
the library has a `.git` that sync did not create, which it only reads. By itself the step writes
nothing, and a failure never fails setup; a setup that did not start exits 1, so the installer
warns. The daemon's `update` and `auto-update` do not run the installers.

### Setup from the environment

`python -m src.user_sync setup --from-env`, which the step also runs whenever one of the first two
variables is set, also under `--yes`, sets sync up without a question:

| Variable | Meaning |
|---|---|
| `AGENTS_USER_SYNC_REPO` | `OWNER/NAME` of a private repository on the GitHub host (`AGENTS_GITHUB_HOST`) |
| `AGENTS_USER_SYNC_REMOTE` | instead, an SSH URL; one on the GitHub host is handled as `OWNER/NAME` |
| `AGENTS_USER_SYNC_NAME`, `AGENTS_USER_SYNC_EMAIL` | the commit identity, required |
| `AGENTS_USER_SYNC_LABEL` | this machine's label, optional |
| `AGENTS_GITHUB_TOKEN` | optional, for a repository on the GitHub host: a token with the `repo` scope |

They count only as the command's process environment gives them: the command line reads them
before the installation's `.env`, removes what `.env` adds and says so, so that an update never
continues a setup. Every command of `python -m src.user_sync` takes `AGENTS_GITHUB_TOKEN` out of
its own environment before anything else, so no child process it starts (git, ssh, ssh-keygen, the
browser) inherits it; only the step and `setup --from-env` receive it, never `installer --summary`.
`init_repo.sh` keeps the token in a variable that is not exported and gives it to the step's
command alone; `install.sh` gives it to `init_repo.sh` alone, never to git. `init_repo.bat` clears
it, because cmd cannot keep a variable from its children, and says that on Windows only a
separate `setup --from-env` reads it. Run that command with the token scoped to it, from the
installation root:

```bash
read -rs t
AGENTS_GITHUB_TOKEN="$t" AGENTS_USER_SYNC_REPO=me/agents-library AGENTS_USER_SYNC_NAME="My Name" \
    AGENTS_USER_SYNC_EMAIL=me@example.com .venv/bin/python -m src.user_sync setup --from-env
unset t
```

The token is checked with GitHub and kept in the secret store like a device-flow sign-in
(`complete_sign_in`), and never printed. With it, or with an account connected earlier, setup adds
this machine's deploy key through the API; otherwise it prints the public key to add by hand and
stops at `waiting_for_access`, and the installer's summary suggests running it again. Setup then
checks access and privacy, prints the preview and starts sync, except that a join that keeps
conflicts stops at `confirmation_needed` with the `preview` and `start --confirm <hash>` commands.
Where privacy cannot be checked, or another host's key needs confirming, it stops with the command
to rerun with `--confirm-private` or `--trust-host-key`. On a machine without the daemon it then
enables the scheduled run (`schedule enable`, every `fetch_minutes`, 5 by default) and says so.
Every command it prints starts with `cd` to the installation, where `-m src…` works.

Running again with the same variables continues a setup that stopped. A setup that it made itself
and that never started may be replaced by a run with another remote: `user-sync-from-env.json` in
the sync state directory records its remote and this machine's public key, which `disconnect`
deletes. Every other setup that has not started is refused, as is a library `.git` while sync has
no settings here, even one left by an earlier sync; the message lists the ways out: finish it with
the wizard, or `disconnect`, move the library's `.git` away and run again. Once sync has started it
changes nothing. The exit code is 1 when setup stopped, sync did not start (`offline`,
`lock_held` and the like, reported as `attention` with that reason) or sync needs attention;
waiting for the deploy key is not an error.

## What syncs

| Synced by default | Never synced |
|---|---|
| `common/**` (personal flows), `personas/**`, `components.json`, `.history/**` | `.lock`, `.tmp-*`, `__pycache__`, `*.pyc`, `.DS_Store` and other file-manager junk |
| `repos/<key>/**` for keys derived from an `origin`, with their personas and history | repository groups without an `origin` (machine-local), `**/.repo.local.json` |
| `.agents-library.json`, `.gitignore`, `.gitattributes`, `.agents-sync/**` | symlinks, files over 5 MiB, names some platform cannot store, anything else at the root |

Scope groups are `common`, `personas`, `history`, `components` and `repos/<key>`; a repository's
shared history segments (`repos/<key>/history/**`) belong to both `history` and `repos/<key>`, and
like `.history/**` they are neither named in commit messages nor counted by the mass-deletion guard. Exclusions live
in the tracked `.agents-sync/scopes.json`, so every machine agrees. Excluding takes the files out
of the repository but never deletes them from any machine. A missing `scopes.json` is restored
from the last commit: losing it must not lift every exclusion. Including a group or file again,
and allowing a file the secret scanner flagged, needs the hash the command prints first, which
covers the list of files it would upload. When another machine includes a group or file again,
this machine's own copies wait for `scope --approve <group>` (or `--approve-file`) here. A
repository group that is new to the library is uploaded and announced in the activity; with
`--ask-new-repositories` it waits for `scope --approve repos/<key>` instead.

Sync carries contents, not permissions: a file another tool committed as executable keeps its mode
in the repository while its content is unchanged, and is written to the library without the
executable bit. The library's own files (`.agents-library.json`, `.gitignore`, `.gitattributes`
and everything under `.agents-sync/`) cannot be excluded.

Files that sync never handles but finds in the remote (a `README.md` added in GitHub's web UI, a
directory a newer version syncs) stay in the repository as they are; they are neither written to
nor deleted from the library. A `.repo.json` written before #169 that still holds a clone path is
split first: the path moves to `.repo.local.json`.

## One cycle

History is linear and holds only what was meant to sync. The local branch points at the last
remote commit this machine integrated. A cycle builds one commit on top of the remote head whose
tree is the three-way merge of that integrated tree, the working tree and the remote tree, filtered
by the merged exclusions; nothing is committed on its own first, so text another machine excluded
never reaches the remote in any commit.

1. `git fetch`, without any lock.
2. Under the library's `.lock`, which every writer of personal flows and component switches takes:
   read the working tree once (scope rules, size limit, secret scanner), merge by the policy below,
   write new history versions and conflict records first and the merged files after them, and
   build the commit. Writers wait only for these local steps, never for the network. A file whose
   bytes changed since sync read it, for example an edit that bypassed the lock, is never replaced:
   it stays, and the next cycle reconciles it as a conflict, again keeping the local text before
   it writes the other version. Windows refuses to replace or delete a file another process holds
   open; sync retries for a moment, and otherwise finishes that file in the next cycle without a
   conflict, since its local text was already settled.
3. `git push`, without any lock. A non-fast-forward rejection starts again, at most three times.

One syncer runs per library: `.git/agents-sync.lock` is taken without waiting, and a second
runner reports `lock_held`. Git lock files that a killed run left behind are removed after ten
seconds. Network calls time out after 60 s and local git calls after 10 s. A network failure
retries after 1, 2, 5, 10 and then every 30 minutes; access and safety failures stop with a reason
until the owner acts.

Commits carry the configured identity and messages such as
`sync(laptop): update user:release-notes, persona builtin:pr-review` with the trailer
`Agents-Sync-Machine: laptop`.

## Conflicts

No text is merged automatically. The version that reached the remote first stays in place, and the
other one is kept.

| Case | Working tree | Kept |
|---|---|---|
| A flow changed differently on two machines | the remote version | the local text as a normal `.history` version, and a conflict record |
| Changed on one machine, deleted on the other | the changed version | a "deletion undone" record; a flow's text, override marker and persona overlay return together |
| The same change, or deleted on both | no conflict | — |
| `.history/**` | union (names are unique) | — |
| Persona overlay, `*.meta.json`, other files | the remote version | the local content inside the record |
| `components.json`, `.agents-sync/scopes.json` | a three-way merge of their sets; it cannot conflict | — |
| Joining a library that has its own flows | matching paths as "changed on two machines", the rest merged | as above; preview required |

Records are `.agents-sync/conflicts/<id>.json`, one per conflict, so records from two machines
never collide; they sync like other files. When `history` is excluded, the kept local version
stays on the machine that lost it. `resolve <id> mine` restores the kept local version with a
normal save (or, for a deletion, a normal delete, which keeps the text in the flow's history);
`keep` and `dismiss` delete the record.

## Joining and safety

| Case | Behavior |
|---|---|
| Empty remote | the first upload, after the owner confirms the preview |
| Foreign content (no `.agents-library.json`) | refused |
| Empty local library | the library is downloaded |
| Local flows and no common history | merged by the policy above, after the owner confirms the preview |
| Remote history rewritten, or reset to an earlier commit | as the previous row: every file from either side survives, after the owner confirms the preview |

- The remote must be empty or hold `.agents-library.json`; a newer `format` stops with
  `format_newer`.
- A commit that would delete at least 10 of the owner's files (flows, personas, switches; history
  and records do not count) and more than half of them waits for the owner's confirmation.
- A public remote is refused before the first upload and checked again daily. A repository of the
  signed-in GitHub account is checked through the API, which also refuses an internal one; other
  remotes get an anonymous `git ls-remote` over HTTPS with an empty configuration, because a
  credential helper would make a private repository look public.
- The secret scanner looks for PEM private keys, GitHub, GitLab, AWS, Slack and `sk-` tokens and
  `password=` in every file about to be committed; a hit keeps that file out of the commit and sets
  `attention: secret`.
- Git runs with sync's own configuration on every call (`GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_NOSYSTEM=1`, explicit `--git-dir` and `--work-tree`, hooks off, no signing, no line
  ending conversion, no credential helper). Inherited `GIT_*` variables are dropped, and so are
  variables named like secrets (tokens such as `AGENTS_GITHUB_TOKEN`, passwords, API keys). SSH uses
  this machine's key only, sync's own `known_hosts` with strict checking, an empty `ssh_config`
  and no agent. The minimum git version is 2.32 (`GIT_CONFIG_GLOBAL`).
- The key is ed25519 with the comment `agents-core-sync:<label>`; the label defaults to the
  platform and a random suffix, never the hostname. `local` is reserved: it names this machine's
  own entries when history is filtered by machine.

## States

`status` reports `off`, `waiting_for_access`, `synced`, `pending` (with a count of changed flows
and other items), `syncing`, `offline` (with `retry_at`), `paused`, or `attention` with a reason:
`auth`, `host_key`, `public_repo`, `unknown_remote`, `secret`, `identity`, `format_newer`,
`git_too_old`, `confirmation_needed`, `new_repository` (flows waiting for approval on this
machine), `scopes_invalid`, `foreign_git`, `library_mismatch`, `library_unreadable`, `git_error`,
`internal` (an unexpected error; the traceback is in `user-sync.log`, and the next cycle tries
again) or `stale`. It also reports the conflict count, the last success, the activity of the
last 20 cycles (sent and received flows, the labels of the machines whose commits a cycle
received, from their `Agents-Sync-Machine` trailers, as `received_from`, and changed non-Markdown
files listed separately) and newly uploaded repository groups. `stale` turns true 24 hours after
the last success, and the state turns
to `attention` after 72 hours.

## Private files

Settings (`user-sync.json`), state (`user-sync-state.json`), this machine's key, `known_hosts`,
the isolated `gitconfig`, an empty hooks directory, `user-sync.log` (1 MiB, three backups) and the
GitHub account's record (`github-account.json`: host, login and where the token is, never the
token itself; `github-token` only in the file fallback) live in a private per-installation
directory, never in the library. The token's entry in the OS secret store is named
`agents-core-sync-<id>`:

- macOS: `~/Library/Application Support/Agents-Core/<id>/user-sync`, inside the daemon's state
  directory (`AGENTS_SERVICE_DIR`, or the directory an installed daemon recorded in
  `data/.shared-service.json`);
- Windows: `%LOCALAPPDATA%\Agents-Core\<id>\user-sync`;
- Linux and others: `$XDG_STATE_HOME/agents-core/<id>/user-sync` (default `~/.local/state`).

`<id>` is the first 16 hex digits of the SHA-256 of the installation path, as for the daemon.
`disconnect` deletes the settings, state and key; the library files and `flows/.user/.git` stay.

## Tests

`tests/test_user_sync.py` runs two libraries as two machines against a local bare repository: the
joining cases, every row of the conflict table, exclusions (checked against every object the remote
holds, not only its last tree), the scanner, privacy, a hostile global git configuration, symlinks,
concurrent saves, push races, offline retries, refused keys, stale git locks, mass deletions and
seeded random edits on both machines. `tests/test_user_sync_github.py` covers the device flow,
token storage and every API call against a fake GitHub on `127.0.0.1`, and
`tests/test_user_sync_github_setup.py` the privacy check, `setup --github`, `add-key`, a new key, port 443
and the wizard, with git going to a local bare repository through a fake SSH command.
`tests/test_user_sync_install.py` covers the installers' step and setup from the environment: the
question, the settings page on a fake daemon, background sync with a fake scheduler, the token
kept from child processes, `.env` ignored, exit codes, reruns and replacing a setup. The real
Keychain, Credential Manager and Secret Service are used only when `AGENTS_TEST_REAL_SECRET_STORE=1`.
The [User sync workflow](../.github/workflows/user-sync.yml) runs these tests on Linux, Windows and
macOS, and sets that variable on the Windows and macOS jobs.
