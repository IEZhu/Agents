# Autodev broker

The run-bound GitHub broker of the self-development schedule (issue #342, epic #336). The design
is [autodev-plan.md](autodev-plan.md), sections 5.1 (who acts), 5.5 (comment markers), 5.6
(session rights and the broker), 5.7 (modes and credentials), 5.8 (public visibility) and 9.6
(labels). The code is `src/schedules/broker.py`; the tests are `tests/test_schedules_broker.py`.

A stage session has no credentials and no network. The broker is its only path to GitHub. It runs
in the runner process, outside the sandbox, and the session reaches it through the runner socket
([autodev-runner.md](autodev-runner.md#runner-socket)) with the thin client `gh`. Every operation is
bound to one run: its issue, its PR, its branch and the pinned `main` commit the run started from.

## Current limits

- **No run opens the broker yet.** The run sequence (#347) creates a `Broker` for each real run,
  registers it with `register(registry, broker)` and calls the runner-internal operations.
- **Two inputs are fixtures until their parts land.** The validated-review record comes with the
  worker launcher (#345) and the pre-mortem record with the run sequence (#347); the broker reads
  them through `validated_review_head(run_dir)` and `read_premortem(run_dir)`, and the tests write
  the fixture files.
- **Earlier run records carry no `issue` or `pr` yet.** The gate's cleanup uses them
  (`bound_numbers`); #347 writes both fields into each run record.

## Socket operations

Only these `gh.*` types are registered, all session-bound (only a process of the current stage
session gets an answer). There is no raw REST or GraphQL request, no merge, close, reopen or
branch deletion, no issue-body edit and no repository-label operation.

| Type | Arguments | What the broker checks |
|---|---|---|
| `gh.push` | `slug`, `head` (40 hex) | The branch is `claude/autodev-<this run's issue>-<slug>`, the same for the whole run. `head` is the promoted `refs/heads/<branch>` of the trusted repository, so only commits imported from the session's bundle can go out. `head` equals the last head a validated review covered. The change check (`src.schedules.paths.check_changes`, from the pinned commit) allows every commit. The diff and the commit messages pass the outgoing-text scan and name no other issue with a closing keyword. |
| `gh.pr_create` | `title`, `body`, `draft` | Once per run, after a push, from the pushed branch to `main`, with `maintainer_can_modify` off. |
| `gh.pr_edit` | `title`, `body` | Only this run's PR; nothing but the title and the body. |
| `gh.comment` | `number`, `body` | Only on this run's issue or PR, with the marker (below). |
| `gh.comment_edit` | `comment_id`, `body` | Only a comment this run logged, or one an earlier run recorded (`records.trusted_comment_ids`) that GitHub reports on this run's issue or PR. |
| `gh.label` | `number`, `label`, `action` (`add` or `remove`) | Only `autodev`, `autodev:in-progress` and `autodev:blocked`, only on this run's issue or PR. The owner's labels (`autodev:ready`, `autodev:next`, `autodev:skip`) are never set or removed. |
| `gh.threads` | none | Lists this run's PR review threads (fixed GraphQL query) with whether each is resolvable. |
| `gh.resolve_thread` | `thread_id` | Only a thread of this run's PR that the broker fetched itself, whose first comment is from an allowlisted bot (login and account ID, `gate.TRUSTED_BOTS`) or is the owner account's and starts with the marker. |
| `gh.read` | `endpoint`, `number` or `sha`, `page` | One of the fixed endpoints under `/repos/<repository>/`: `issue`, `issue_comments`, `issue_events`, `pull`, `pull_reviews`, `pull_comments`, `pull_files`, `pull_commits`, `commit_status`, `check_runs`. |

GraphQL is sent only by the broker, only with the two documents in `DOCUMENTS` (the thread query
and the `resolveReviewThread` mutation), compared by identity, so no session text becomes a
GraphQL document. REST paths are built by the broker and must lie under `/repos/<repository>/`.

**Closing keywords** (`close`, `fix`, `resolve` and their forms, followed by `#N`, `GH-N`,
`OWNER/REPO#N` or an issue or PR URL) may name only this run's issue in this repository. The
check covers PR titles and bodies, comments and the commit messages of a push.

**Comment markers** (design 5.5). Every comment's first line is `<!-- autodev -->`. A comment
without it, with any line starting with `/agent`, or with text the scan finds secret-like is
refused. The only unmarked comments are the exact bot commands in `BOT_COMMANDS`
(`@coderabbitai review`, `@coderabbitai full review`), and only on this run's PR; a near variant
(other spacing, case, a trailing word or newline) is an ordinary comment and needs the marker.

## Runner-internal operations

These are methods of `Broker`, never registered with the socket, so a session cannot request
them.

- `cleanup(actions)` executes the gate's actions (#338): it removes `autodev:in-progress`, after
  the marked comment the action carries, only on an issue or PR that an earlier run record names.
- `select(body)` writes the selection comment (`<!-- autodev -->` then `<!-- autodev:selection -->`)
  on this run's issue and adds `autodev:in-progress`; it returns the comment ID for the run
  record's `comments`.
- `bind_resume(pr)` binds a `resume` run to an open self-development PR of this run's issue
  (label `autodev`, branch `claude/autodev-<issue>-…` of the repository itself).
- `create_risk_issues(finding_ids)` opens risk issues (Q3) from findings of the run's validated
  pre-mortem record: level `medium` and above, at most 3 per run, title and body from the record
  and scanned, the label `autodev:risk`, the marker and a link to this run's PR. Security
  classification is runner-side (design 5.8): a finding with a security mark, or one whose title
  or body matches the fixed keyword rule `SECURITY_KEYWORDS`, stays local. A finding counts as
  security-marked unless the record says `"security": false`.

## Credentials and modes

- **Push** with the deploy key from `github.deploy_key` in `autodev.json` and the pinned
  `github.com` host keys in `github_known_hosts` beside it, through
  `GIT_SSH_COMMAND="ssh -F /dev/null -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile=<known hosts> -o StrictHostKeyChecking=yes"`
  with `SSH_AUTH_SOCK` unset and git's clean configuration (`src.schedules.gitsafe`), as
  `push --porcelain --no-follow-tags --no-signed git@github.com:<repository>.git <sha>:refs/heads/<branch>`.
- **API writes** with the fine-grained token from the keychain item named by `github.token`
  (`src.schedules.github`), never the `gh` token, and without following redirects.
- **A real run** (`run`, `resume`) refuses to start (`CredentialsMissing`) unless the key and the
  host keys are regular files (not symbolic links) and the token can be read. The token and the
  key are known secrets of the outgoing-text scan and are masked in the log.
- **`dry-run`** makes the broker read-only: every write, also of the runner-internal operations,
  is refused before anything is sent.

The network, git, the keychain and the record readers are injected, so the tests never touch
GitHub, ssh or the keychain.

## Broker log

`<run_dir>/broker.jsonl` holds one JSON object per line, masked with `records.mask`: `intent`
before each write, then `done` with the created or changed object's ID (the PR, comment or issue
ID, the pushed head or the thread ID) or `failed`, and `refused` for refusals that reach a check.
A write that a closing socket interrupts stays visible as an `intent` without `done`.
