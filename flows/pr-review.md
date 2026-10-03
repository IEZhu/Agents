---
persona:
  agent: code_reviewer
  skills: [skill-content-structure, skill-dev-clean-code, skill-dev-testing, skill-dev-security, skill-git-conventions]
  implants: [implant-chain-of-verification, implant-regression-first]
---
# Review and merge a pull request or merge request

The `code_reviewer` persona contributes review judgment for checking findings.
This flow still fixes, commits, replies and merges: the persona's "flag, don't
fix", its positive findings and its Output Format do not apply here, and an
unattended run decides instead of asking.

Follow this flow to take a GitHub PR or GitLab MR through review, fixes,
validation, and merge. Read the target's applicable repository instructions.
For Agents-Core, read [AGENTS.md](../AGENTS.md) and the
[session playbook](../docs/session-playbook.md) for repository working practices.
Use the target project's checks; [the test guide](../tests/README.md) covers Agents-Core.
Use the target's branch naming, commit style, PR/MR template, CI, merge method
and required approvals. Agents-Core source references and helpers are optional
supporting material; they do not supply those conventions for another project.

## Invoke and identify the target

```text
Run flows/pr-review.md for <PR or MR URL>.
```

Through Agents-Core MCP, call
`run_flow(flow="pr-review", request="Review <PR or MR URL>")`. Carry user
constraints such as `no-merge` into `request` and use the returned `repo_path` as
the target. Source links belong to the MCP installation; repository operations
and instructions belong to the target. Confirm the PR/MR matches that repository
before making changes.

Optional constraints include a specific review scope or `review-only` / `no-merge`.
Without a URL, locate the current branch's existing PR/MR. If the task includes
submission and none exists, create it from the prepared branch after the scope
check in step 1 and the session's own review in step 3, because opening it
starts the bot reviews. Ask for the target only when the repository or branch is
ambiguous.

An explicit user invocation of this flow authorizes the review work, necessary
fixes, commits, pushes, PR/MR updates, review requests, thread replies, resolution,
in an interactive run one follow-up issue for findings moved out of the PR under
[Keep the cycle converging](#keep-the-cycle-converging), and merge when the
conditions below are met. When the task includes submission, it also authorizes
one PR/MR per independent part of a split branch (step 1). An explicit
`review-only` or `no-merge` instruction disables merge. A request only to submit
a PR/MR for review does not authorize merge. Other user restrictions and
repository protections still apply.

Record the invocation language for the final report. Titles, descriptions, and
review replies must be English. Run the flow in the current model session.
Write committed documentation in English under the
[documentation language policy](documentation-refresh.md#documentation-language).

## 1. Establish the current state

- Confirm the platform, repository, source and target branches, remote head SHA,
  local checkout, and PR/MR state. Preserve unrelated changes and use an isolated
  worktree when needed. If already merged or closed, report that state.
- Read the full diff, title, description, all review activity, project checks,
  merge rules, and required approvals. Confirm which bot integrations actually
  exist and which repository or account configuration is visible.
- Keep a working list of every finding, its source, decision, fix commit, reply,
  and validation evidence. Carry it across rounds for the final report.
- Check the scope before the first review round. Each bot pass reports a few
  findings, so a large PR that mixes
  [production code](#keep-the-cycle-converging) with tooling, evaluation data
  or documentation takes many rounds. When this flow creates the PR from such a
  branch and its parts do not depend on each other, open one PR per part, each
  against the target and production code first, and take each through this
  flow, unless the user asked for a single PR or the run is the
  [issue agent's](issue-agent.md), which keeps one pull request per issue. Keep
  the scope of a PR under review fixed: merge no branch or PR into it other than
  the target, and put a change outside its scope in its own PR.

### Keep the branch current and git healthy

Check this when the flow starts, before requesting reviews for a new head, after
every wait for reviews, and before the final report. Another merge into the
target branch can make a reviewed head unmergeable while the bots are working.

1. Identify the remote that holds the source branch (the fork for a fork PR,
   from `headRepository` on GitHub) and fetch it and the target branch. Read the PR/MR mergeability
   (on GitHub `gh pr view N --json mergeable,mergeStateStatus,headRefOid`;
   `UNKNOWN` means GitHub is still computing it: wait briefly and read again).
2. Compare the remote head with the head this session last pushed or reviewed.
   Commits the session did not make (from the owner, another session or a bot)
   are inspected first and kept; never rewrite over them unseen.
3. When the source branch conflicts with the target (`CONFLICTING`/`DIRTY`), or
   a change merged into the target touches the same files or behavior, update
   the branch: rebase it onto the target, or merge the target into it when the
   repository forbids rewriting history or the branch is shared. Resolve each
   conflict by keeping the intent of both sides: read the merged change, adapt
   this branch to new names and interfaces, and update tests that both sides
   touched. Run the relevant checks, then push to the source repository's remote
   that step 1 verified, naming the destination and the lease explicitly:
   `git push <source remote> --force-with-lease=refs/heads/<branch>:<last seen head> HEAD:refs/heads/<branch>`
   after a rebase, or the same push without the lease after a merge. Never rely
   on the default remote or upstream, which may point at the target repository
   for a fork PR.
4. The updated branch is a new head: earlier reviews do not cover it. Update the
   description when the scope changed, request reviews again as in
   [Obtain reviews](#3-obtain-reviews-for-the-current-head) and record what was
   resolved.
5. Stop and report a blocker, without guessing, when a conflict cannot be
   resolved with confidence (both sides change the same behavior in
   incompatible ways, or the resolution needs a decision outside this PR's
   scope). Name the conflicting files and the commits on each side, leave the
   branch as it was, and state how to resume after a manual fix (for the issue
   agent, `/agent review`).

Treat other git problems the same way: detect, inspect, report; never override.
A push rejected by the lease or by branch protection, a missing or renamed
source branch, a detached HEAD, unexpected local changes, a failing rebase or a
hook that rejects the commit is evidence to read, not an obstacle to bypass.
Abort a failed update so the branch is left as it was (`git rebase --abort`,
or `git merge --abort` for a merge-based update), keep unrelated local changes,
never use plain `--force`, `--no-verify` or a reset that discards work, and
never delete or recreate a branch to get past a rejection.

## 2. Keep the title and description accurate

Use a short English title that names the resulting change. Write a concise
English description explaining the concrete problem, final behavior, and relevant
validation. Follow the repository template when present. Describe the final diff
for a reviewer who has not seen the conversation.

After **every commit**, compare the title and description with the complete
current diff. Update them when the scope, behavior, or validation has changed.
Remove obsolete claims and abandoned approaches. A wording update does not
substitute for reviewing the new commit.

## 3. Obtain reviews for the current head

Review the diff yourself first, as a reviewer would: correctness, edge cases,
error handling, concurrency and platform differences (a lock that only works on
POSIX, for example), security and tests. Do this for the whole PR before the
first bot request of this flow, and for every commit the session adds before it
is pushed. A PR from [issue implementation](issue-implementation.md) had this
review before it opened, so review only the commits added since. Fix what you
find and record it in the working list like a bot finding: a bot reports a few
findings per pass, so each problem left for the bots costs a round.

The expected setup runs CodeRabbit and Copilot automatically on the initial
GitHub PR. Confirm that those reviews actually started. CodeRabbit normally
reviews subsequent pushes automatically, but its quota is often exhausted.
After each push of fixes, once the remote head updates, **explicitly
re-request Copilot review when the integration is available and Copilot is not
paused** under [Quota, rate limits and errors](#quota-rate-limits-and-errors),
using the current platform's supported action. Do not assume a push requests it
again. Confirm that a review was queued for the current head. If an earlier
review is still running, wait for it to finish, then request again if the current
head has no queued or completed review. A successful API response alone does not
prove that a new review started.
If expected automation did not start, check its status and request review through
the installed integration's supported action.

Keep Copilot at the owner's **Lite** review effort. Each Copilot review overview
states its effort ("Review effort: Lite" or "Balanced"). Since 2026-09-28
GitHub's default is Balanced unless Lite was selected explicitly, and Balanced
reviews with a higher-reasoning model: in 2026-10 they used up the owner's
Copilot quota within three days. When a Copilot review of the PR ran at Balanced,
request no further Copilot reviews, treat Copilot as unavailable, and report it
at once, with where the owner selects Lite under "Review effort level": the
personal Copilot settings (Copilot, then Code review) for reviews requested with
the owner's account, which this flow's requests are, and the repository's or
organization's settings (Copilot, then Code review) for automatic reviews.
Resume after the owner confirms the switch. Do not change the effort, the
settings or billing yourself, and do not guess a request parameter for the
effort: there is no assumed `lite` CLI flag.

On GitLab, inspect the actual reviewers and integrations available to the MR.
Use supported platform actions for requests and discussions. Do not assume that
GitHub Copilot or the GitHub helpers below exist on GitLab. Record absent bots.

For each available bot, distinguish these states:

| State | Action |
|---|---|
| Review queued, running, or not yet visible | Wait and check again; silence is not approval |
| Review completed for an earlier head | Obtain a review covering the current head |
| Current-head review has findings | Read and handle every finding |
| Current-head approval or completed review with no actionable findings | Record completion for that bot and head |
| Explicit quota exhaustion, rate limit, service failure, or unavailable integration | Record the evidence, pause that bot as described below, and continue with the other available bots |

### Verify completion evidence

Before declaring a bot's review complete, re-read the remote head and the full
review output covering that exact SHA. For a bot that publishes a separate
review status or check, also require its latest run for that head to finish
successfully. On GitHub, inspect commit statuses as well as check runs; a bot
may use either. For a bot without a separate run status, a substantive completed
review of the whole PR, including an explicit no-findings result, is sufficient.

For CodeRabbit, verify its successful current-head status or check, completed
walkthrough and reviewed commit range. A `pending` / `in_progress` status for
the current run or a walkthrough saying review is still in progress requires
further waiting, even when all existing threads are resolved.

A bot may create an empty `COMMENTED` review on the current SHA when replying
in an older thread. That review, or a reply confirming one finding is fixed,
does not establish that the bot completed its review of the whole PR. A
successful status alone does not clear actionable findings. If the available
signals disagree, investigate and keep the review pending. Explicit
unavailability is handled under the rules below. Record the head, evidence
links and completion times in the working record and final report.

Use bounded waits with backoff and keep the user informed of meaningful changes.
A wait timeout alone does not establish quota exhaustion or unavailability.
Inspect failed requests and bot status before retrying. If every bot is
unavailable, handle all existing findings and continue to the
[merge conditions](#5-merge-when-ready), whose item on changes no bot reviewed
says which of them need an independent review. Unavailability is not approval.

### Quota, rate limits and errors

Recognize these bot responses by their text (observed on this repository's PRs in
2026-09 and 2026-10). A quota or error response is not a review of the head: it
neither approves nor clears findings.

| Bot | Evidence | Meaning | Next action |
|---|---|---|---|
| CodeRabbit | A comment marked `rate limited by coderabbit.ai`: "Review limit reached. Next included review available in N minutes." | Rate limit with a stated wait | Pause CodeRabbit until the time the comment was last updated plus N minutes: CodeRabbit rewrites this text into its long-lived summary comment, so its creation time is too early. After that, if no review of the current head has started, request one with a PR comment `@coderabbitai review`, once. A rate-limit reply to that request starts a new pause. |
| CodeRabbit | A reply to `@coderabbitai review`: "Action not completed" and "Review rate limited.", with the status `Review rate limited`; after a push, the status alone | The same rate limit; the reply states no wait | Read the wait from CodeRabbit's summary comment on the PR, which then says "Review limit reached" and "Next included review available in N minutes", and pause as in the row above. When no comment states a wait (seen in 2026-10 after a push), pause for one hour from the status time. |
| CodeRabbit | The status `Review paused`, and "Reviews paused" in its summary comment | Not a quota pause: CodeRabbit is available but stopped reviewing new pushes on its own because the branch received many commits | Do not record a pause. Request one review of the current head with `@coderabbitai review` when the head needs one, and wait for it like any other review. Do not send `@coderabbitai resume`, which reviews every later push. |
| Copilot | A review body "...the user who requested the review has reached their quota limit." | Account quota exhausted; no reset time is given (in 2026-09 it returned within days, not at a fixed date) | Pause Copilot. While paused, request a review at most once per 24 hours, and only when the current head still needs one. A normal review ends the pause. |
| Copilot | "Copilot encountered an error and was unable to review this pull request." | Transient failure | Re-request once after a few minutes. After a second failure on the same head, treat Copilot as unavailable for this round. |

Review allowances are shared across PRs. CodeRabbit's review attempts of the past
seven days set its hourly allowance (in 2026-10, 93 attempts set it to one review
per hour), and a rate-limited request appeared to count as an attempt too.
Request a review only for a head that needs one; batching fixes into one push per
round, as in [Resolve findings](#4-resolve-findings-and-repeat), keeps those
heads few.

Keep each pause with its evidence (the comment or review link), the time it was
seen and the earliest next attempt. Read the current time from the clock
(`date -u`), never from memory or comment timestamps. `next_attempt` is the
evidence time plus the stated wait (for Copilot quota, the last review request
plus 24 hours), and a pause has ended only when the measured current time is at
or after it. Record it where the next session will find
it: the [issue agent](issue-agent.md#3-state) keeps it in the issue's state
comment; an interactive session reports it and keeps it in its working notes.
While a bot is paused, continue with the other bots, do not re-request the
paused bot on every push, and do not wait for it to complete the cycle. When the
pause ends and the current head has no review from that bot, request one. A
review round that ends with a bot still paused lists that bot as unavailable,
with its evidence and next attempt time, in the final report.

### Optional GitHub helpers from the Agents-Core installation

Run helpers from the target GitHub repository context after checking their source.
When the target is another repository, use the absolute helper paths from the
Agents-Core source checkout while keeping the working directory at the target.
Do not assume `scripts/dev/` exists in the target. If the installation's files
are inaccessible, use the platform API or CLI directly.
Use the platform API or CLI directly by default. If using the optional helpers,
derive the source checkout from the built-in flow's location. When `flow.source`
is `builtin`, the parent of `flow.source_path` from MCP is the checkout's
`flows/`. A `user:` or `repo:` copy has its own path, so take `source_path` from
the `builtin:pr-review` entry of `list_flows()` or from
`get_flow("builtin:pr-review")` instead. Set `AGENTS_CORE_ROOT` to that checkout's
absolute path and `TARGET_REPO` to the selected target checkout. Verify both
paths before running commands, and select an available Python interpreter for
the helper. Replace `OWNER`, `REPO`, and `N` with the verified target:

```bash
AGENTS_CORE_ROOT=/absolute/path/to/Agents-Core
TARGET_REPO=/absolute/path/to/target-repository
cd "$TARGET_REPO"
gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
bash "$AGENTS_CORE_ROOT/scripts/dev/wait_copilot.sh" N
python "$AGENTS_CORE_ROOT/scripts/dev/pr_threads.py" N
```

[wait_copilot.sh](../scripts/dev/wait_copilot.sh) waits for a Copilot review on
the PR's current head after it matches the source branch tip. It does not check
approval or decide whether findings remain. It counts any Copilot review object
on the head, and GitHub records a quota-limit response as one, so its success
line can follow a quota response. Read the review body and apply
[Quota, rate limits and errors](#quota-rate-limits-and-errors) before you count
the head as reviewed.
[pr_threads.py](../scripts/dev/pr_threads.py) lists unresolved threads and review
bodies on that head. Its output truncates text and omits intermediate comments;
fetch full comments, discussions, and review bodies through the platform before
deciding a finding is handled. Also read PR conversation comments and bot status.

## 4. Resolve findings and repeat

A round is one head that the bots review, with their reviews of it. Handle a
round's findings together: start when each available bot has finished reviewing
the current head or is paused, and push the round's fixes once, because each
pushed head the bots review is a new round.

1. Read all new comments, inline threads, and full review bodies, including
   findings present only in summaries. Include outstanding findings from earlier
   heads. A review's overview or approval does not erase an unresolved issue.
2. Check each finding against the code, intended behavior, and relevant tests.
   Reproduce the problem when practical. Fix real issues; decline an inapplicable
   suggestion with a concrete reason and evidence. If a comment is vague, request
   the specific file, line, and failure instead of inventing a fix. From the
   third round on, decide each real finding as in
   [Keep the cycle converging](#keep-the-cycle-converging).
3. Make the necessary changes and run the relevant project checks. Use the
   [documentation flow](documentation-refresh.md) when public behavior or AI
   instructions need corresponding documentation updates.
4. Inspect the diff, review the fixes as new code as in
   [Obtain reviews](#3-obtain-reviews-for-the-current-head), commit them, and
   check title/description alignment.
   [Keep the branch current](#keep-the-branch-current-and-git-healthy) before
   pushing, so that one push carries the round's fixes and any branch update.
   Confirm the remote head matches the pushed commit. Do not count an
   older review or an earlier passing check as verification of the new head.
   If CI intentionally skips a check by changed-file filters, verify that its
   code, tests, and configuration are unchanged since the last passing run.
   Report that run and the skipped scope explicitly; required checks still apply.
5. Reply to each finding in its own thread in concise English. State the
   conclusion, fix commit, and useful evidence. For a declined finding, state
   the reason. Use ordinary punctuation, with **no em dashes or en dashes**.
   Do not rate or praise the finding. Examples:

   - `Fixed in abc1234. The parser now rejects an empty name. The focused tests pass.`
   - `No change needed. This path already checks ownership before reading the record.`

6. Resolve a thread only after its answer is posted and the issue is fixed, moved
   to the PR's Follow-ups, or its rejection is justified. For a finding in a
   review body without a thread,
   respond in the platform's corresponding discussion, identifying the finding.
   Do not mark unresolved human approval requirements as satisfied by a reply.
7. Re-request Copilot after every push of fixes when the integration is
   available and Copilot is not paused, using the current platform's supported
   action. Request a review from a paused bot only once its recorded next attempt
   time has passed. Check CodeRabbit's automatic review or explicit quota result.
   Wait for the available bots to review the current head, read the new results,
   and repeat when there are actionable findings.

On GitHub, `python "$AGENTS_CORE_ROOT/scripts/dev/pr_threads.py" N --resolve-mine`
(with the source root and target working directory established above) resolves threads
whose last comment belongs to the authenticated account. Inspect the candidate
threads first: that condition alone does not prove they were handled correctly.

The review cycle is complete when all available bots have approved or completed
a review of the current head with no remaining actionable findings, and every
earlier finding has been handled: fixed, declined with a reason, or moved to the
PR's Follow-ups. It also ends when no bot can review because each is explicitly
unavailable or out of quota, after existing findings are handled.
If one bot is unavailable, continue with every bot that can still review.

### Keep the cycle converging

A bot reports a few findings per pass, and every fix is new code to review, so a
large PR can run many rounds without converging: in 2026-10, a PR of about 3,400
added lines took 14 Copilot rounds with one to four findings in each. Count
rounds over the PR's whole history, earlier sessions included: each head with a
completed bot review is one. From the third round on, decide each real finding
by its impact. Start from the bot's severity label (CodeRabbit's Major or Minor,
Copilot's high or medium, for example) and judge the impact yourself.

- Fix in this PR a blocker or major finding, a security finding, a finding that
  can silently corrupt results or data whatever its label, a finding in
  production code, and a regression that one of this PR's fixes caused.
  Production code is what the project ships or runs for its users: in
  Agents-Core, `src/`, installers and scripts, and the agents, skills, implants,
  rules and flows it serves. Tests, evaluation harnesses and data, and
  documentation for people are not.
- Move every other finding out of the PR: list it with its link under
  "Follow-ups" in the description, reply in its thread that it moved there, and
  resolve the thread. An interactive run also opens one follow-up issue with
  that list and links it under "Follow-ups"; a later session adds to the same
  issue. The issue agent only lists them. A round whose findings all move out
  needs no push and ends the cycle.
- Make each fix the smallest change that removes the problem, with a regression
  test that fails without it where the change can be tested, and review it before
  pushing as in [Obtain reviews](#3-obtain-reviews-for-the-current-head). In the
  2026-10 PR above, a late fix for a leftover download directory broke
  concurrent downloads on Windows.

When fixes to one component keep producing findings, its design is the likely
cause: say so in the report and propose a simpler design instead of another
patch.

## 5. Merge when ready

Re-read the remote state immediately before merging. Confirm all of the following:

- The current head is the commit that was checked and handled in the final round.
- Required project checks pass, and relevant local validation has passed.
- Every actionable finding is fixed or moved to the PR's Follow-ups; each
  declined finding has an explanation.
- Threads have been answered and resolved as appropriate; required approvals
  are satisfied and no conflicts or other merge blockers remain.
- The title and description reflect the final diff, and merge is authorized.
- When the bots are paused or unavailable, the changes since the newest head
  that a bot fully reviewed have had an independent review if they touch code,
  tests or configuration. Leave out what came in unchanged from the target
  branch, and after a rebase compare the commits with `git range-diff`. A fresh
  subagent, given that diff without this session's reasoning, reviews it, and
  its findings are handled as in step 4; fixes for them get the session's own
  review, not another subagent round. The session's own review of its fixes
  does not replace this one. Without a subagent tool, do not merge: report those
  changes and the earliest next bot attempt. A `no-merge` run skips this check
  and reports the changes no bot reviewed.

Merge using the project's permitted merge method, then verify the platform reports
the PR/MR as merged and record its merge commit or resulting revision. Never
bypass branch protection, required approvals, or failing checks. If a blocker
remains, report it precisely and leave the request open. A pending review or
pending check requires further waiting, not a claim that the flow is complete.

## 6. Report the outcome

Reply in the language of the original invocation. Include:

- The PR/MR link, merged status and revision, or the exact remaining blocker.
- **All changes made in response to review**, across every round, grouped briefly
  by behavior or file. Include fix commits where they help trace the result.
- Every declined finding and its reason, and the findings moved to Follow-ups,
  with the follow-up issue when one was opened.
- Tests and required checks, their results, and any validation limitations.
- Any unavailable bots, quota failures, or a Copilot review effort other than Lite,
  and the changes that no bot reviewed, with the review that covered them.

For `review-only` or `no-merge`, report that merge was intentionally omitted.
Distinguish verified approval, completed review without findings, and unavailable
reviewers. Preserve the working record until this report is complete.
