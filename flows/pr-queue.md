---
persona:
  agent: code_reviewer
---
# Take several pull requests through review in one sequence

Use this flow when more than one open pull request or merge request has to reach
the target branch one after another. It orders the queue, proves the order
merges cleanly, runs [pr-review](pr-review.md) per item, and hands the owner one
chain to merge. [pr-review](pr-review.md) governs each item; this flow adds only
what a single-request review cannot know: the order, the interaction between the
items, and what changes after each merge.

Follow [bounded execution and evidence reuse](../docs/flow-execution.md): one
working record for the whole queue, batched reads, and one monitor per pending
run. Apply the
[bounded-return gate for your client class](../docs/mcp-tool-safety.md#bounded-return-gate-by-client-class)
from the first large call on.

The git steps below are platform independent. The platform commands are
GitHub's, because that is where they were derived; on GitLab use the equivalent
of each, or the API, and record which ones the target does not offer: merge
request approval rules and the project's protected branches in place of
rulesets, a pipeline run on a branch in place of `workflow_dispatch`, and the
merge request's own approval state in place of `reviewDecision`. Do not assume a
GitHub helper exists there.

## Invoke and identify the queue

```text
Run flows/pr-queue.md for <PR or MR URLs>.
```

Through Agents-Core MCP, call
`run_flow(flow="pr-queue", request="Take <URLs> to the target branch in sequence")`
with `current_persona`, and apply its `persona_activation` (`code_reviewer`) as
a switch first. Carry user constraints such as `no-merge` or `review-only` into
`request`, and use the returned `repo_path` as the target.

Without a list, take every open request against the target branch that the user
named, and state which ones you included. A request that targets another branch
is not part of the queue: name it as excluded.

Resolve every item to its exact current head SHA and to the repository that
holds it, and record both. A request opened from a fork has its head in that
fork, where the target's ordinary remote refs do not reach it, so the SHA and
its remote are what the later steps fetch and merge. A branch name is not an
item's identity: it moves while the queue is being worked.

**This flow never merges on its own initiative, and it never widens
authorization.** Each item's merge follows
[pr-review step 5](pr-review.md#5-merge-when-ready) under that flow's
conditions and the same authorization: an explicit `no-merge` or `review-only`
invocation disables merge for every item, and a run without merge authorization
reports the chain for the owner instead. Being next in a queue is not consent to
merge an item.

## 1. Order the queue and record why

Decide the order before touching any branch, and record it with one reason per
item. The working record carries it through every round and into the report.

- **A fix the other items need goes first.** A dependency in code, a lockfile or
  configuration fix that turns a shared check green, or a rename the others
  build on. In 2026-10 `Vulnerability scan` was red on every open pull request
  of this repository because the target branch's own lock carried an advisory;
  only the item that bumped the lock could clear it, so that item led the queue.
- **An item that touches a shared table or an index document goes last.** Two
  items that add adjacent rows to the same table conflict even when their
  subjects do not: in 2026-10 two requests edited neighbouring rows of
  `docs/README.md`, and the second had to be updated after the first merged.
- **Otherwise order by size, smallest first**, so the early merges are cheap to
  verify and a late failure costs less rework.
- **A release-cutting target changes the unit of readiness.** Where every merge
  cuts a release (a version bump, a tag, then a deployment of that tag), the
  next item can only be rebased after the previous item's bump commit has
  landed, and an item counts as done when its tag pipeline produced its
  artifacts, not when the request merged. Wait for that evidence before
  starting the next item.

## 2. Simulate the chain before reviewing it

Build the whole queue on a throwaway branch cut from the queue's own target
branch, in the order of step 1, and record every conflict with its resolution.
Disable `rerere` for the simulation: a stored resolution from an earlier attempt
hides a conflict that the real merge will raise.

Fetch the target and every item's head first. An item opened from a fork has its
head in that fork, not in the target's remote, so fetch each item's own remote
(on GitHub, `headRepository` of the request names it) rather than assuming one.
That is one fetch per item: two items on the same remote are two refs, and
fetching the remote once leaves the second head missing, so its recorded SHA
cannot be merged.

```bash
git fetch <target remote> <target branch>
git fetch <item remote> <item head ref>        # once per item, not per remote
git switch -c tmp/queue-simulation <target remote>/<target branch>
git -c rerere.enabled=false merge --no-ff --no-edit <first item head>
```

Then merge the remaining items one at a time, in the order of step 1. Do not
stop at the first conflict: resolve it, record which item owns that resolution,
complete the merge and continue, so one pass covers the whole queue instead of
the prefix before the first conflict. Resolve by keeping the intent of both
sides, exactly as
[pr-review](pr-review.md#keep-the-branch-current-and-git-healthy) requires. A
conflict found here is work for that item's own round, not for the simulation
branch.

The simulation branch outlives this step: step 3 pushes it under a temporary
name to run the integrated checks. Delete it only after step 3 has finished with
it, and leave it first, because git refuses to delete the branch that is checked
out:

```bash
git switch --detach <target remote>/<target branch>
git branch -D tmp/queue-simulation
git push <target remote> --delete tmp/queue-integration   # if step 3 pushed it
```

Repeat the simulation after every merge (step 5). Never push the simulation to
the target branch or to any item's branch.

### More than one merge base

For each item, count the merge bases against the base it will actually have:

```bash
git merge-base --all <item head> <its base>
```

More than one base means Git's ort merge can resolve the item with a virtual
base while GitHub still reports the request as conflicting, because GitHub
evaluates a single base. This happens when an item was rebuilt on another
item's head and also merged the target. Plan a merge of the target into that
item right after its predecessor lands: it leaves one base and clears the
reported conflict. Record which items need it, so the step is not rediscovered
after the predecessor merges.

## 3. Check the integrated tree, not only each item

A check that is red on every item because of the target branch's own state
cannot be cleared by any single item, and a check that passes on each item
separately can still fail on the merged result. Run the workflows the queue
changes on the integrated tree:

1. Push the simulation of step 2 to a temporary branch, for example
   `tmp/queue-integration`, but only when every item in it comes from the target
   repository itself. A request opened from a fork carries whatever its author
   committed, a workflow file included, and pushing those commits into the
   target runs that workflow there, on a `push` trigger, with the target's own
   secrets, before any item has been reviewed. When the queue holds a fork
   request, do not push the simulation: run the integrated check in a repository
   that has none of the target's secrets, or take the evidence from the ordinary
   runs as in the next item. Record which of the two was used, and never relax
   this because an item looks harmless.
2. Start the workflow on that branch with `workflow_dispatch`, for example
   `gh workflow run vulnerability-scan.yml --ref tmp/queue-integration`. This
   works only for a workflow that declares the `workflow_dispatch` trigger on
   the repository's default branch. When it does not, say so in the report and
   take the evidence from the ordinary runs instead: the predecessor's own run
   once it has merged, and the next item's run after it merges the new target.
3. Record the run link, the exact commit it ran on and its result in the working
   record.
4. Delete the temporary branch, locally and on the remote.

Name the result in each affected item's round: a check that the queue fixes is
reported with the integrated run that proves it, and with the item that carries
the fix. Do not report an item as green on a check that is still red on its own
head.

This run is evidence about exactly the commits it merged, before any review
round. A fix pushed to any item, and any merge into the target, replaces the
tree it tested, so the claim does not carry over: re-simulate and run it again
when a head changes, and before the final report. Record which heads the run
covered, so a stale run is recognisable rather than reused.

## 4. Run pr-review for each item, in order

Take each item through [pr-review](pr-review.md) completely before starting the
next one: its own review, the bot rounds, the thread replies, the description,
and its merge conditions. Two items under active review at once produce heads
that no simulation covers.

Keep each item's scope fixed, as
[pr-review step 1](pr-review.md#1-establish-the-current-state) requires: merge
no other item's branch into it. The queue is expressed by the order and, when
the user asked for it, by the bases of step 6, never by merging the items into
each other.

## 5. After each merge, re-establish the queue

A merge changes the target branch, so everything the queue proved before it is
evidence about a tree that no longer exists. After each merge:

1. Fetch the target branch from the remote that step 1 verified holds it, not a
   remote named by habit, and confirm the platform reports the item as merged
   with its merge commit. Fetching the wrong repository leaves the simulation
   and the merge-base counts below describing a target that has not moved.
2. Fetch every remaining item's head from the remote that holds it and re-resolve
   its exact head SHA. A branch, a fork's especially, can have moved while the
   earlier items were reviewed, and a simulation built on a head that is no
   longer the request's head proves nothing about the request.
3. Re-simulate the remaining items on a fresh throwaway branch (step 2) and
   re-count their merge bases.
4. Run step 3's checks again on that new simulation. The merge changed the
   target, so the remaining integrated tree is not the one the earlier run
   passed, and the earlier run says nothing about it. Report no item as ready on
   a run that covered a different tree.
5. Merge the new target into every item that step 2 marked, and into any item
   the platform now reports as conflicting.
6. The updated items have new heads: their earlier reviews do not cover them, so
   request reviews again as
   [pr-review step 3](pr-review.md#3-obtain-reviews-for-the-current-head)
   requires before counting them ready.
7. Report the next item with its head, its checks and what is left.

## 6. Stack the bases only after each head is proven

Stacking means giving each item the previous item's branch as its base, so the
platform shows one chain and each diff stays its own. It is optional, and it
costs coverage while it lasts:

- An automated reviewer configured per base branch stops reviewing the stacked
  items. In this repository `.coderabbit.yaml` lists `^dev$` under
  `reviews.auto_review.base_branches`, so a request based on a feature branch is
  not reviewed automatically; it needs one explicit `@coderabbitai review` per
  head, against the hourly allowance
  ([pr-review](pr-review.md#quota-rate-limits-and-errors)).
- A workflow whose `pull_request` trigger filters base branches does not run.
- A base change does not fire `pull_request` for the types the workflows listen
  to, so a check skipped while stacked does not come back on its own.

Therefore restack only after each item's current head already has its completed
bot reviews and its passing checks, and record per item the run that covered
that exact head. Changing the base leaves the head commit untouched, so a check
that builds and tests that commit alone still describes what will be merged. A
check whose result depends on the base does not, and most do: GitHub's default
`pull_request` checkout is the merge of the head into the base, so a run against
the old base tested a tree that no longer exists. Identify those checks per
item, say that their evidence is stale while the item is stacked, and read them
again once the item is back on the target branch. State in the report which checks
will not re-run while the item is stacked, which run covered the head, and which
of them depend on the base. When the user did not ask for stacked bases, leave
every item on the target branch and express the queue through the order and the
report alone.

```bash
gh pr edit <item> --base <predecessor branch>
```

A predecessor that lives only in a fork cannot be a base: the option selects a
branch of the target repository, so it either fails or, worse, selects an
unrelated branch of the same name there. Leave such an item unstacked, and say
which items the stack therefore skips.

When the predecessor merges and its branch is deleted, GitHub retargets an open
request whose base was that branch to the predecessor's own base, which in this
chain is the queue's target. That is a consequence of the deletion, not of the
merge: when the branch is kept, or the predecessor was merged somewhere else,
the retarget does not happen. Read each stacked item's base after every merge,
set it back by hand where the platform did not, and re-read the item's checks
before counting it ready again.

## 7. Approvals, and who may merge

Read the target's actual rules rather than assuming them
(`gh api repos/<owner>/<repo>/rulesets` and the ruleset's own endpoint on
GitHub). Record for the whole queue: which refs the rules cover, how many
approvals they require, which checks they require, and who may bypass them.

A rule that requires an approving review is not satisfied by the author: an
account cannot approve its own request. Whether that forces a bypass depends on
who else may approve and on how many approvals the rule wants, so count the
eligible approvers from the rule and the repository's access and compare that
count with the required one: one approval from one other person does not satisfy
a rule that requires two. Where enough eligible approvers exist, their approvals
satisfy the rule and no bypass is needed, and the report asks for those reviews
instead; where they do not, the report names the approvals still missing and the
bypass that would cover them. Only when the author is the sole eligible approver does each merge need
that account's own bypass, and the report then says so once for the queue
instead of leaving it to be rediscovered per item. Never bypass a rule yourself,
and never present a bypass as a satisfied approval.

A rule set on the target branch does not cover an item stacked on a feature
branch (step 6). Such an item can report itself as mergeable while the rule
still applies to whichever item targets the protected branch. Say that in the
report; do not read it as the queue having become easier.

## 8. Superseded items

An item whose change another item now carries is closed only on the owner's
word, with a comment that names the replacement request. Until then it stays
open and keeps its place in the queue, or is reported as excluded with the
reason.

## 9. Report the chain

Reply in the language of the invocation. Give the owner one table and one list:

| Order | Request | Head | Checks | Reviews | Remaining blocker |
|---|---|---|---|---|---|

- The merge chain in order, each item with its head SHA, the merge command, and
  the bypass note of step 7 where it applies.
- The simulation result: the conflicts found, which item owns each resolution,
  and the items that need a target merge after their predecessor lands (step 2).
- The integrated-tree runs of step 3, with what each proves and which item
  carries the fix.
- Per item, everything [pr-review step 6](pr-review.md#6-report-the-outcome)
  requires, and for a stacked item the checks that will not re-run with the run
  that covered its head.
- What is not verified: a check that could not run, a bot that stayed paused
  with its next attempt time, and any item left blocked with its exact reason.

State plainly that the merges are the owner's to make, and preserve the working
record until this report is delivered.
