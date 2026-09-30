# Review and merge a pull request or merge request

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
submission and none exists, create it from the prepared branch. Ask for the
target only when the repository or branch is ambiguous.

An explicit user invocation of this flow authorizes the review work, necessary
fixes, commits, pushes, PR/MR updates, review requests, thread replies, resolution,
and merge when the conditions below are met. An explicit `review-only` or
`no-merge` instruction disables merge. A request only to submit a PR/MR for review
does not authorize merge. Other user restrictions and repository protections
still apply.

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

The expected setup runs CodeRabbit and Copilot automatically on the initial
GitHub PR. Confirm that those reviews actually started. CodeRabbit normally
reviews subsequent pushes automatically, but its quota is often exhausted.
After each fix commit is pushed and the remote head updates, **explicitly
re-request Copilot review when the integration is available**, using the current
platform's supported action. Do not assume a push requests it again.
Confirm that a review was queued for the current head. If an earlier review is
still running, wait for it to finish, then request again if the current head has
no queued or completed review. A successful API response alone does not prove
that a new review started.
If expected automation did not start, check its status and request review through
the installed integration's supported action.

Keep Copilot in the user's configured **lite mode**. Check available repository
and integration settings to confirm the mode; preserve that configuration.
There is no assumed `lite` CLI flag. If the mode cannot be inspected, report that
limit and use the established integration without changing its mode or billing.

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

Use bounded waits with backoff and keep the user informed of meaningful changes.
A wait timeout alone does not establish quota exhaustion or unavailability.
Inspect failed requests and bot status before retrying. If every bot is
unavailable, handle all existing findings and continue to the merge conditions;
unavailability is not approval.

### Quota, rate limits and errors

Recognize these bot responses by their text (observed on this repository's PRs in
2026-09). A quota or error response is not a review of the head: it neither
approves nor clears findings.

| Bot | Evidence | Meaning | Next action |
|---|---|---|---|
| CodeRabbit | A comment marked `rate limited by coderabbit.ai`: "Review limit reached. Next included review available in N minutes." | Rate limit with a stated wait | Pause CodeRabbit until the comment time plus N minutes. After that, if no review of the current head has started, request one with a PR comment `@coderabbitai review`, once. |
| Copilot | A review body "...the user who requested the review has reached their quota limit." | Account quota exhausted; no reset time is given (in 2026-09 it returned within days, not at a fixed date) | Pause Copilot. While paused, request a review at most once per 24 hours, and only when the current head still needs one. A normal review ends the pause. |
| Copilot | "Copilot encountered an error and was unable to review this pull request." | Transient failure | Re-request once after a few minutes. After a second failure on the same head, treat Copilot as unavailable for this round. |

Keep each pause with its evidence (the comment or review link), the time it was
seen and the earliest next attempt. Compute times from the clock (`date -u`), not
from memory or other timestamps: `next_attempt` is the evidence time plus the
stated wait, and a pause has ended only when the measured current time is at or
after it. Record it where the next session will find
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
derive the source checkout from the loaded flow's location (`flow.source_path`
from MCP, whose parent is `flows/`). Set `AGENTS_CORE_ROOT` to that checkout's
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
approval or decide whether findings remain.
[pr_threads.py](../scripts/dev/pr_threads.py) lists unresolved threads and review
bodies on that head. Its output truncates text and omits intermediate comments;
fetch full comments, discussions, and review bodies through the platform before
deciding a finding is handled. Also read PR conversation comments and bot status.

## 4. Resolve findings and repeat

1. Read all new comments, inline threads, and full review bodies, including
   findings present only in summaries. Include outstanding findings from earlier
   heads. A review's overview or approval does not erase an unresolved issue.
2. Check each finding against the code, intended behavior, and relevant tests.
   Reproduce the problem when practical. Fix real issues; decline an inapplicable
   suggestion with a concrete reason and evidence. If a comment is vague, request
   the specific file, line, and failure instead of inventing a fix.
3. Make the necessary changes and run the relevant project checks. Use the
   [documentation flow](documentation-refresh.md) when public behavior or AI
   instructions need corresponding documentation updates.
4. Inspect the diff, commit the fixes, check title/description alignment, and
   push. Confirm the remote head matches the pushed commit. Do not count an
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

6. Resolve a thread only after its answer is posted and the issue is fixed or
   its rejection is justified. For a finding in a review body without a thread,
   respond in the platform's corresponding discussion, identifying the finding.
   Do not mark unresolved human approval requirements as satisfied by a reply.
7. Re-request Copilot after every pushed fix commit when the integration is
   available, using the current platform's supported action. Check CodeRabbit's
   automatic review or explicit quota result. Wait for the available bots to
   review the current head, read the new results, and repeat when there are
   actionable findings.

On GitHub, `python "$AGENTS_CORE_ROOT/scripts/dev/pr_threads.py" N --resolve-mine`
(with the source root and target working directory established above) resolves threads
whose last comment belongs to the authenticated account. Inspect the candidate
threads first: that condition alone does not prove they were handled correctly.

The review cycle is complete when all available bots have approved or completed
a review of the current head with no remaining actionable findings, and every
earlier finding has been handled. It also ends when no bot can review because
each is explicitly unavailable or out of quota, after existing findings are handled.
If one bot is unavailable, continue with every bot that can still review.

## 5. Merge when ready

Re-read the remote state immediately before merging. Confirm all of the following:

- The current head is the commit that was checked and handled in the final round.
- Required project checks pass, and relevant local validation has passed.
- Every actionable finding is addressed; each declined finding has an explanation.
- Threads have been answered and resolved as appropriate; required approvals
  are satisfied and no conflicts or other merge blockers remain.
- The title and description reflect the final diff, and merge is authorized.

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
- Every declined finding and its reason.
- Tests and required checks, their results, and any validation limitations.
- Any unavailable bots, quota failures, or unverified review-mode configuration.

For `review-only` or `no-merge`, report that merge was intentionally omitted.
Distinguish verified approval, completed review without findings, and unavailable
reviewers. Preserve the working record until this report is complete.
