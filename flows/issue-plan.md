# Issue agent: plan and replan

Called by the [issue agent](issue-agent.md) for `@agent plan`, `@agent replan`
and the planning half of `@agent run`. It turns an issue into a verified,
reviewable implementation plan and publishes it as a Markdown comment. It never
changes the repository.

## 1. Understand the request

1. Read the whole issue: title, body, every comment and linked issues or PRs.
   For `replan`, also read the previous plan and the owner's requested changes.
2. Read the target repository's instructions and the code, documents and tests
   the request touches. Record the files and behavior you verified, with paths.
3. Restate the goal and the expected result in one or two sentences each.

## 2. Validate the requirements

Check that the request is:

- **Unambiguous:** one reasonable interpretation, or the choice does not change
  the result.
- **Feasible:** possible in this repository with the session's tools and network,
  without secrets or services the session cannot reach.
- **Consistent:** compatible with the repository's rules, existing behavior and
  earlier decisions in the thread.
- **Testable:** has observable completion criteria.

If a gap changes what gets built, stop and ask. Post the questions as a numbered
list, each explaining why it matters and offering a recommended default. Record
the comment in `questions_comment_id`, set the phase to `needs_info` and end.
The owner's next `@agent ...` answer resumes planning from this step. Do not ask
about things you can determine from the code or the thread.

## 3. Write the plan

Use this structure, in the issue's language, with paths and identifiers in English:

```markdown
<!-- issue-agent -->
<!-- issue-agent:plan v2 -->
## Plan v2

**Goal:** ...  **Result:** ...

<details open><summary>Steps</summary>

1. ... (files: `path/a.py`, `path/b.md`)
2. ...
</details>

<details><summary>Tests and checks</summary> ... </details>
<details><summary>Risks and pre-mortem</summary> ... </details>
<details><summary>Out of scope</summary> ... </details>

**Done when:** observable criteria.
**Changes since v1:** (replan only) ...

To execute: `@agent run_plan v2`. To change it: `@agent replan <what to change>`.
```

The **pre-mortem** assumes the change has shipped and caused a problem, then
lists the most likely causes (regressions, data loss, security, compatibility,
missed callers, wrong assumptions) with the step or test that prevents each.
Keep the plan proportional to the task; a one-line fix needs a short plan.

For a plan with several independent parts, you may use the Workflow tool to
explore parts in parallel; keep the final plan in one comment.

## 4. Publish

1. Post the plan as a new comment. A replan posts a new version; it never edits
   an older plan, so each version stays reviewable.
2. Immediately update the state: `plan.version`, `plan.comment_id`, and
   `plan.issue_updated_at` set to the issue's current `updated_at`; phase `planned`.
   Whenever a later step needs the plan, treat the newest comment carrying
   `<!-- issue-agent:plan vN -->` as authoritative if it is newer than the state
   entry (a session can end between posting and saving), and repair the state.
3. For `@agent run`, continue directly with
   [issue-implementation](issue-implementation.md) using this version.
