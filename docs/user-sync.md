# User library sync

Status: the engine and its command line are implemented (#165). The daemon loop (#167), scheduled
runs and the terminal wizard (#168), GitHub sign-in (#166), the web UI (#170) and installer
integration (#171) are separate parts of the [epic #173](https://github.com/IEZhu/Agents/issues/173).

Sync keeps the personal library (`flows/.user`, or `AGENTS_USER_FLOWS_DIR`) the same on a user's
machines through one private git repository with one branch, which is never force-pushed. The
library becomes the working tree of a git repository at `flows/.user/.git`; nothing else in the
installation changes.

## Command line

Every runner uses `src/user_sync/` (standard library and the git CLI only). Without the daemon:

```bash
python -m src.user_sync setup --remote git@github.com:me/agents-library.git \
    --name "My Name" --email me@example.com [--label laptop] [--ask-new-repositories]
# add the printed public key to the repository as a deploy key with write access
python -m src.user_sync check      # access, and the remote is empty or an Agents-Core library
python -m src.user_sync preview    # what would be uploaded and downloaded, and the conflicts
python -m src.user_sync start --confirm <hash from preview>
python -m src.user_sync run        # one cycle; --force ignores the retry delay
python -m src.user_sync status
python -m src.user_sync conflicts
python -m src.user_sync pause      # resume continues
python -m src.user_sync disconnect
python -m src.user_sync configure [--fetch-minutes 1-60] [--[no-]ask-new-repositories]
python -m src.user_sync resolve <conflict id> keep|mine|dismiss
python -m src.user_sync scope [--exclude GROUP] [--include GROUP] [--exclude-file PATH] \
    [--include-file PATH] [--allow-secret PATH] [--approve repos/<key>] [--confirm HASH]
```

Each command takes `--json`. `--state DIR` and `--library DIR` before the command name the private
state directory and the library explicitly; scheduled runs pass both, so they never depend on the
scheduler's environment. The command line reads the installation's `.env` like the MCP servers,
and holds the installation's shared session lease while it runs, so an update never replaces the
code under it. Setup records the library; a run on another library stops with
`library_mismatch`.

Hosts other than github.com print their host key fingerprints at setup; confirm one with
`--trust-host-key SHA256:…` after comparing it with the host's published fingerprint. github.com's
keys are refreshed from its API on every setup. Where privacy cannot be checked over HTTPS,
`--confirm-private` records the owner's confirmation. Setting up another remote starts a new
history there: one root commit with the current files, so old commits never travel to it.

## What syncs

| Synced by default | Never synced |
|---|---|
| `common/**` (personal flows), `personas/**`, `components.json`, `.history/**` | `.lock`, `.tmp-*`, `__pycache__`, `*.pyc`, `.DS_Store` and other file-manager junk |
| `repos/<key>/**` for keys derived from an `origin`, with their personas and history | repository groups without an `origin` (machine-local), `**/.repo.local.json` |
| `.agents-library.json`, `.gitignore`, `.gitattributes`, `.agents-sync/**` | symlinks, files over 5 MiB, names some platform cannot store, anything else at the root |

Scope groups are `common`, `personas`, `history`, `components` and `repos/<key>`. Exclusions live
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
   it writes the other version.
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
- A public remote is refused before the first upload and checked again daily: an anonymous
  `git ls-remote` over HTTPS with an empty configuration, because a credential helper would make a
  private repository look public. GitHub's API check arrives with #166.
- The secret scanner looks for PEM private keys, GitHub, GitLab, AWS, Slack and `sk-` tokens and
  `password=` in every file about to be committed; a hit keeps that file out of the commit and sets
  `attention: secret`.
- Git runs with sync's own configuration on every call (`GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_NOSYSTEM=1`, explicit `--git-dir` and `--work-tree`, hooks off, no signing, no line
  ending conversion, no credential helper) and inherited `GIT_*` variables are dropped. SSH uses
  this machine's key only, sync's own `known_hosts` with strict checking, an empty `ssh_config`
  and no agent. The minimum git version is 2.32 (`GIT_CONFIG_GLOBAL`).
- The key is ed25519 with the comment `agents-core-sync:<label>`; the label defaults to the
  platform and a random suffix, never the hostname.

## States

`status` reports `off`, `waiting_for_access`, `synced`, `pending` (with a count of changed flows
and other items), `syncing`, `offline` (with `retry_at`), `paused`, or `attention` with a reason:
`auth`, `host_key`, `public_repo`, `unknown_remote`, `secret`, `identity`, `format_newer`,
`git_too_old`, `confirmation_needed`, `new_repository` (flows waiting for approval on this
machine), `scopes_invalid`, `foreign_git`, `library_mismatch`, `library_unreadable`, `git_error`
or `stale`. It also reports the conflict count, the last success, the activity of the
last 20 cycles (sent and received flows, changed non-Markdown files listed separately) and newly
uploaded repository groups. `stale` turns true 24 hours after the last success, and the state turns
to `attention` after 72 hours.

## Private files

Settings (`user-sync.json`), state (`user-sync-state.json`), this machine's key, `known_hosts`,
the isolated `gitconfig`, an empty hooks directory and `user-sync.log` (1 MiB, three backups) live
in a private per-installation directory, never in the library:

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
seeded random edits on both machines. The [User sync workflow](../.github/workflows/user-sync.yml) runs these tests on Linux,
Windows and macOS.
