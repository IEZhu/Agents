I'll give you the points. First, though, you should know that your own numbers are the strongest argument your colleague has. If you go into the retro trying to "shut it down" without dealing with that, you'll probably lose the room, and you'd deserve to.

## The problem with your position

A 19-hour median to first review means half your PRs wait longer than that just to get a first look, before any rework or re-review. At about 25 PRs a week across 6 people, that's a lot of work sitting idle. It also pushes people toward bigger, batched PRs, which get worse reviews. Your colleague isn't arguing for "merging unreviewed code" for its own sake. They're arguing that the current gate costs a lot of time and may not buy much safety.

So you're really choosing between two things:
- **(A)** keep pre-merge review as it is
- **(B)** their post-merge proposal
- **(C)** keep pre-merge review but fix the latency

Option C is probably the one nobody has put on the table yet.

## Points that hold up against the proposal

1. **"Reviewed within a day afterwards" relies on the same discipline you don't have now.** If reviews take 19 hours when they block a merge, they'll probably slip further when nothing is waiting on them. Post-merge review tends to decay into no review unless something enforces it. Ask: what happens when a post-merge review is 3 days late, and who tracks that?
2. **Main is shared. A bad merge hits everyone right away.** Feature flags stop untested behavior from reaching users. They don't stop broken builds, bad shared-code refactors, dependency bumps, logging or config changes, performance regressions in code paths outside the flag, or secrets committed to history. None of those sit neatly behind a flag.
3. **"Risky stuff gets blocking review" assumes people classify risk correctly.** The dangerous changes are often the ones that looked trivial: a query change that drops an index, a "small" serializer tweak that changes an API contract. Ask who decides what counts as risky and what happens when someone gets it wrong.
4. **Flag debt is a real cost.** Putting most changes behind flags means more flag combinations to test, stale flags to clean up, and dead code paths. Ask whether you have flag tooling and a cleanup process, or whether this adds them as a hidden requirement.
5. **Fixing a problem after merge costs more than catching it before.** Once code is on main, other work builds on it. A post-merge review comment turns into a follow-up PR, which is easier to put off and easier to skip.
6. **Review is also knowledge sharing.** With 6 people, pre-merge review may be the main way anyone other than the author learns a change happened. Post-merge review keeps that in theory, but only if people actually do it.
7. **Compliance and audit.** If you have any SOC 2, ISO, or customer contract requirement for "changes reviewed before production," post-merge review may break it. Check this before Thursday. If it applies, it could settle the question.

## Steelmanning their side (expect these points)

- Small PRs merged quickly behind flags is a well-established trunk-based development practice, not a fringe idea.
- Long review queues cause their own problems: context switching, merge conflicts, larger batches, and reviewers rubber-stamping big diffs anyway.
- A blocking review that arrives 19 hours later on a 30-line change may be adding delay without adding much safety.
- They've already carved out migrations and auth, so this isn't "no review at all."

If you can't answer these, "I don't get the appeal" will come across as defending the status quo.

## What I'd bring to the retro

Don't aim to shut it down. Reframe it: "We agree the 19-hour wait is the problem. Let's fix that first, then decide if we still need post-merge review." Concrete proposals:
- A review SLA (for example, first response within 4 working hours) with a rotating reviewer-of-the-day
- A PR size limit, since small PRs get reviewed faster
- Pairing counts as review for small changes
- Review the SLA in a month. If latency is still bad, trial post-merge review on a narrow, clearly defined category (docs, tests, internal tooling) with tracked follow-up

That gives your colleague the speed they want and keeps the safety you care about. It's also much harder to argue against than a flat no.

**Confidence: MEDIUM.** Points 1 to 5 are sound reasoning in general, but I don't know your incident history, CI coverage, flag tooling, or compliance situation. **Reconsider** if your CI and test coverage are strong, you've rarely caught serious bugs in review, and you already run feature flags well. In that case your colleague's proposal is a lot more defensible than it looks to you right now.

One bias to watch in yourself: you've asked for points to shut this down, not to decide it. That's confirmation bias, and it will show if the team pushes back.
