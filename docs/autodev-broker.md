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

- **One broker per real run (#347).** The run sequence ([autodev-sequence.md](autodev-sequence.md))
  builds the `Broker` after the gate, unbound (`Run.issue` None) with the credentials, the run
  directory, every stage path a session may write as `session_writable`, the decoys with
  `decoys.trip` as `on_decoy`, and `pr_allowed` reporting an open security risk of the run's
  pre-mortem; it registers it on the stage sessions' socket with `register(registry, broker)`,
  binds it to the selected issue (`bind_issue`) or the resumed PR (`bind_issue`, then
  `bind_resume`) and calls the runner-internal operations below. A dry-run builds no broker, so
  it writes nothing and leaves no `broker.jsonl`.
- **Two inputs come from the worker launcher.** The worker launcher (#345,
  [autodev-workers.md](autodev-workers.md)) writes the findings ledger and the validated-review
  record after an accepted review and the pre-mortem record after an accepted pre-mortem; the broker
  reads them through `validated_review_head(run_dir)` (`review_gate`: open only while no accepted
  review left an open blocker in the ledger) and `read_premortem(run_dir)`. A finding's `security`
  mark is the runner's own: `workers.GATE_SECURITY` for a review finding and `SECURITY_KEYWORDS`
  for a pre-mortem risk, applied by the worker service when it writes the records; the stage
  output carries only counts, so no mark comes from the session.
- **A secret known later reaches the scan through `add_known`.** The Codex API key of a worker's
  decision exists only after the broker; `Broker.add_known` adds it to the outgoing-text scan
  and the log masking, and the run sequence passes it as the worker service's `on_known`.
- **Run records carry `issue` and `pr`.** The run sequence writes both (and the `comments` it
  wrote, each `{"id", "kind", "number"}`), which the gate's cleanup (`bound_numbers`) and
  `Broker.state` read.
- **The state comment's writer is the run sequence.** It calls `Broker.state` after every submit
  stage: a new comment for a new PR, an edit of the state comment an earlier run record names for
  a resumed one. No socket operation can write or edit an `<!-- autodev:…` sub-marker.

<a id="open-decision-pushing-a-resume-run-after-main-moved-347"></a>

## Decision: pushing a resume run after `main` moved (#347)

A resume run's broker is bound to the merge base of the PR branch and the pinned `main` when the
branch no longer descends from `pinned` (`Run.base`, chosen by the run sequence), so the change
check, the outgoing scan and the bundle import of the resumed branch work against the commits the
branch itself added (#347 open question Q-resume-push, default adopted); the path lists are still
read at `pinned` (`TrustedGit(lists_at=pinned)`). There is still no force
push: merging `main` in is refused as a merge commit, and a rebased branch is refused by GitHub as
a non-fast-forward push. A conflict with the base therefore ends the submit stage with
`review_status: blocked` for the owner, and the history stays append-only. A resume run whose PR
head is not the head the trusted repository imported (someone else pushed to the branch) is
blocked before any session (Q-resume-head).

## Socket operations

Only these `gh.*` types are registered, all session-bound (only a process of the current stage
session gets an answer). There is no raw REST or GraphQL request, no merge, close, reopen or
branch deletion, no issue-body edit and no repository-label operation.

| Type | Arguments | What the broker checks |
|---|---|---|
| `gh.push` | `slug` (at most 60 characters), `head` (40 hex) | The branch is `claude/autodev-<this run's issue>-<slug>`, the same for the whole run. `head` is the promoted `refs/heads/<branch>` of the trusted repository, so only commits imported from the session's bundle can go out. `head` equals the last head a validated review covered. The change check (`src.schedules.paths.check_changes`, from the pinned commit) allows every commit. The diff and the commit messages pass the outgoing-text scan, and the commit messages name no other issue with a closing keyword. |
| `gh.pr_create` | `title`, `body`, `draft` | Once per run, after a push, from the pushed branch to `main`, with `maintainer_can_modify` off. |
| `gh.pr_edit` | `title`, `body` | Only this run's PR; nothing but the title and the body. |
| `gh.comment` | `number`, `body` | Only on this run's issue or PR, with the marker (below). |
| `gh.comment_edit` | `comment_id`, `body` | Only a comment this run's `gh.comment` wrote (a `done` entry of the broker log). Never the selection comment, a state comment or any comment an earlier run recorded. |
| `gh.label` | `number`, `label`, `action` (`add` or `remove`) | `autodev` may only be added, and only to this run's PR; `autodev:blocked` may be added or removed on this run's issue or PR. Removing `autodev` and setting or removing the claim `autodev:in-progress` are runner-internal (`select`, `cleanup`), because the gate's WIP limit and claim depend on them. The owner's labels (`autodev:ready`, `autodev:next`, `autodev:skip`) are never set or removed. |
| `gh.threads` | none | Lists this run's PR review threads (fixed GraphQL query) with whether each is resolvable. |
| `gh.resolve_thread` | `thread_id` | Only a thread of this run's PR that the broker fetched itself, whose first comment is from an allowlisted bot (login and account ID, `gate.TRUSTED_BOTS`) or is the owner account's and starts with the marker. GraphQL names a bot author with `__typename` `Bot` and a login without `[bot]` (`coderabbitai`, account ID 136622811, checked on PR #365); the broker appends `[bot]` to a Bot's login before the allowlist check, and a User node never counts as a bot. A Bot without an integer `databaseId` never matches, and `gate.trusted_bot` requires an allowlisted login with its integer ID. |
| `gh.read` | `endpoint`, `number` or `sha`, `page` | One of the fixed endpoints under `/repos/<repository>/`: `issue`, `issue_comments`, `issue_events`, `pull`, `pull_reviews`, `pull_comments`, `pull_files`, `pull_commits`, `commit_status`, `check_runs`. The answer is `{"data": <GitHub's JSON>, "next_page": <whether the Link header names a next page>}`. |

GraphQL is sent only by the broker, only with the two documents in `DOCUMENTS` (the thread query
and the `resolveReviewThread` mutation), compared by identity, so no session text becomes a
GraphQL document. REST paths are built by the broker and must lie under `/repos/<repository>/`.

**Closing keywords** (`close`, `fix`, `resolve` and their forms, after any character but a
letter or digit, so `_Closes #12_` counts, followed by `#N`, `GH-N`, `OWNER/REPO#N` or an issue or
PR URL) may name only this run's issue in this repository. GitHub honours them in the PR
description and in commit messages, so the check covers PR titles and bodies and the commit
messages of a push, not its diff (a change to code or tests that holds `Fixes #341` can be
pushed). Comments are checked too, which GitHub does not require; it is defence in depth.

**Comment markers** (design 5.5). Every comment's first line is `<!-- autodev -->`. A comment
without it, with any line starting with `/agent`, or with text the scan finds secret-like is
refused. The only unmarked comments are the exact bot commands in `BOT_COMMANDS`
(`@coderabbitai review`, `@coderabbitai full review`), and only on this run's PR; a near variant
(other spacing, case, a trailing word or newline) is an ordinary comment and needs the marker.

**Mentions and sub-markers.** Outside that exact bot command, no comment, comment edit, PR title
or body, or risk issue may hold an `@` mention (of a user, team, bot or app; only an ASCII e-mail
local part before `@` makes it none, and `@coderabbit…` or `@copilot…` is refused even there), because a mention can command a bot (`@coderabbitai resolve`, `@copilot …`) outside the
broker's checks. Nor may it hold an `<!-- autodev:…` sub-marker: state and selection comments are
runner-internal. Both are checked on the text as written, after NFKC normalisation with
zero-width and other invisible format characters removed, and with HTML entities decoded
until nothing changes (`&#64;` and `&amp;#64;` render as `@`).

**Closing and deadlines.** A session's write checks the socket's `closing()` again right before it
is sent, after the checks and scans. `gh.threads` and `gh.resolve_thread` start a GraphQL call only
while a whole API timeout (`TIMEOUT`, 30 s) still fits before the request's deadline, so a slow
GitHub answers "ask again" instead of outlasting it.

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
  and scanned, the label `autodev:risk`, the marker and a link to this run's PR. The cap and a
  per-finding deduplication count every attempt in `broker.jsonl` except one GitHub refused with
  an HTTP status (a network error may follow a created issue, so it counts); a broker reads the
  log back when it opens, so a second broker of the same run cannot exceed them. Security classification
  is runner-side (design 5.8): a finding with a security mark, one whose title or body matches the
  fixed keyword rule `SECURITY_KEYWORDS` after NFKC normalisation and removal of invisible
  characters, or one whose title holds a non-ASCII letter (a homoglyph could hide a keyword),
  stays local. A finding counts as security-marked unless the record says `"security": false`.

## Credentials and modes

- **Push** with the deploy key from `github.deploy_key` in `autodev.json` and the pinned
  `github.com` host keys in `github_known_hosts` beside it, through
  `GIT_SSH_COMMAND="ssh -F /dev/null -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile=<known hosts> -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes"`
  (a key or host-keys path with whitespace is refused, since ssh splits `UserKnownHostsFile`
  at whitespace) with `SSH_AUTH_SOCK` unset and git's clean configuration (`src.schedules.gitsafe`), as
  `push --porcelain --no-follow-tags --no-signed git@github.com:<repository>.git <sha>:refs/heads/<branch>`.
- **API writes** with the fine-grained token from the keychain item named by `github.token`
  (`src.schedules.github`), never the `gh` token, and without following redirects.
- **A real run** (`run`, `resume`) refuses to start (`CredentialsMissing`) unless the key and the
  host keys are regular files (not symbolic links) and the token can be read. The token and the
  key are known secrets of the outgoing-text scan and are masked in the log.
- **The run directory** must belong to the runner and lie neither inside nor around a path the
  session can write (`session_writable`, checked with `socket_service.check_directory` when the
  broker opens; `UnsafeRunDirectory` otherwise), because the broker trusts the review and
  pre-mortem records and its own log there.
- **`dry-run`** makes the broker read-only: every write, also of the runner-internal operations,
  is refused before anything is sent.

The network, git, the keychain and the record readers are injected, so the tests never touch
GitHub, ssh or the keychain.

## Broker log

`<run_dir>/broker.jsonl` holds one JSON object per line, masked with `records.mask`: `intent`
before each write, then `done` with the created or changed object's ID (the PR, comment or issue
ID, the pushed head or the thread ID) or `failed`, and `refused` for refusals that reach a check
(including the early push refusals). A failed GraphQL mutation is logged as `failed`. A label
removal that GitHub answers with 404 (the label is already gone) is logged as `done` with the note
`already absent`, and the gate's cleanup continues with its next action.
A write that a closing socket interrupts stays visible as an `intent` without `done`.
