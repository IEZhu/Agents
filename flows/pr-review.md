# Review and merge a pull request or merge request

Follow this flow to take a GitHub PR or GitLab MR through review, fixes,
validation, and merge. Read [AGENTS.md](../AGENTS.md) and the
[session playbook](../docs/session-playbook.md) for repository working practices.
Use the target project's checks; [the test guide](../tests/README.md) covers Agents-Core.

## Invoke and identify the target

```text
Run flows/pr-review.md for <PR or MR URL>.
```

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
re-request Copilot review**. Do not assume a push requests it again.
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
| Explicit quota exhaustion, service failure, or unavailable integration | Record the evidence and continue with the other available bots |

Use bounded waits with backoff and keep the user informed of meaningful changes.
A wait timeout alone does not establish quota exhaustion or unavailability.
Inspect failed requests and bot status before retrying. Avoid repeated requests
after an explicit quota failure. If every bot is unavailable, handle all existing
findings and continue to the merge conditions; unavailability is not approval.

### GitHub helpers in this repository

Run helpers from the target GitHub repository context after checking their source.
Replace `OWNER`, `REPO`, and `N` with the verified target:

```bash
gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
scripts/dev/wait_copilot.sh N
python scripts/dev/pr_threads.py N
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
7. Re-request Copilot after every pushed fix commit. Check CodeRabbit's automatic
   review or explicit quota result. Wait for the available bots to review the
   current head, read the new results, and repeat when there are actionable findings.

On GitHub, `python scripts/dev/pr_threads.py N --resolve-mine` resolves threads
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
