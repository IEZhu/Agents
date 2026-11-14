# Outbound timing rule: A/B results over three runs

Date: November 7 to 10, 2026. Rule: `rules/rule-outbound-timing.mdc` on branch
`claude/outbound-timing-trial`, two texts, each snapshotted beside the run that used it as
`rule-as-run.mdc`: 1,009 bytes for runs 01 and 02, 1,096 bytes for run 03. Contexts built at
`a85f49a2`. These results describe those texts and the recorded models and settings only.
The rule is not adopted and this report recommends against adopting it.

## Question

The rules layer checks what an outbound draft says and never how it lands on the person who
will read it. `outbound-timing` states that when the user will send text to someone outside
the session, the recipient's reception is part of the deliverable: name in one line when it
lands and how it reads on arrival, leave the choice to the user, refuse to guess the
recipient's clock, send now when a deadline runs or a date must go on record or the
recipient asked or the content is a time-critical warning, and never edit the message to
suit a moment.

## Method

The [component ablation harness](../evals/ablation/README.md), three runs. Both arms always
use the production context of the case's agent, and the `without` arm only cuts the rule's
section: 942 characters in every pair of runs 01 and 02, 1,029 in every pair of run 03,
with no other difference and no build errors in any run.

| Run | cases | groups | what changed from the previous run |
|---|---:|---|---|
| 01 | 21 | `timing-wait` 6, `timing-now` 5, `control` 10 | first run |
| 02 | 26 | `ownership` 8, `timing-now` 6, `control` 12 | new case set, same rule text |
| 03 | 26 | identical to run 02, same file hash `ff83b971` | same cases, one clause of the rule |

Run 03's edit scopes the length cap to the timing note: "at most one line" became "one line
on the moment, no more ... That cap is on the timing line alone, not on what else the draft
needs." All 26 `without` contexts in run 03 are byte-identical to run 02's by `ctx_sha256`,
and every `with` context differs from run 02's by exactly the 87 characters of the edit, so
run 03 against run 02 is a per-case comparison of one change.

Answers and blind pairwise judging in both orders by Claude Code agents on the session
model (Opus 5), no model pinned. 73 pairs in total, so 146 answer files, two per case, and
146 verdict files, two per pair because each pair is judged in both orders: 42 and 42 in run
01, 52 and 52 in each of runs 02 and 03, with none missing in any run. An adversarial checker
reviewed each case set before any judging.

The case set of runs 02 and 03 is
[`outbound_timing_cases.json`](../evals/datasets/outbound_timing_cases.json). The rule texts,
the three run directories and the gates registered before each run live on branch
`claude/outbound-timing-trial`, each run directory holding its `GATES.md` and the
`rule-as-run.mdc` that produced it. Gates were registered before each run's answers existed,
and for runs 02 and 03 before the case set was in place.

## The finding that governs every other number

Between runs 02 and 03, **14 of the 26 cases changed their robust outcome**, and 9 of those
14 are control cases. The control group is where the rule provably does nothing: in both
runs, reading all 12 `with` answers, it added no remark about when to send in any of them.
Its group net moved from -5 to +6, an eleven point swing with no effect to cause it, and
three quarters of its cases changed side.

The `without` contexts were byte-identical between those two runs, so that swing is answer
and judge sampling alone. Run 03's `ownership` net of +2 against run 02's -2 is a four point
move inside a null group's eleven point band, and its robust count of 4 with against 3
without against run 02's 3 against 4 is one case. Neither is a signal.

The same scale makes run 01's `timing-wait` figure of +10 with 5 robust wins and none
against the largest effect seen anywhere here, but its case set was later shown not to
discriminate on its own hypothesis, and its rubrics rewarded the shape the rule produces
rather than grading its clauses.

The practical consequence: at 8 to 12 cases per group this harness cannot resolve an effect
of this size, and the verdict based gates were never going to settle the question. The
checks that carried information in all three runs are the ones established by reading every
answer.

## Results against the registered gates

| Run | group | net | robust with / without | gate |
|---|---|---:|---:|---|
| 01 | `timing-wait` | +10 | 5 / 0 | pass |
| 01 | `timing-now` | +6 | 4 / 1 | fail as written, intent held |
| 01 | `control` | -3 | 2 / 3 | fail as written, intent held |
| 02 | `ownership` | -2 | 3 / 4 | **fail** |
| 02 | `timing-now` | +8 | 5 / 1 | pass |
| 02 | `control` | -5 | 3 / 5 | pass |
| 03 | `ownership` | +2 | 4 / 3 | **fail** |
| 03 | `timing-now` | +7 | 4 / 1 | pass |
| 03 | `control` | +6 | 5 / 3 | pass |

Gate 1 asked for net above 0, at least 5 robust wins of 8, and no robust losses. It was
never met, in either attempt with rubrics that grade the rule's own clauses.

## What holds across all three runs

**The send-now carve-out.** Reading every `with` answer in the group: 5 of 5 in run 01, 6 of
6 in run 02, 6 of 6 in run 03 say to send now and give a reason, and none advises waiting.
In run 02 four of six explicitly refuse the pull the case planted, and in run 03 the same
pattern holds. The single robust loss in each of runs 02 and 03 is the same case, and in both
the judge recorded that each arm refuses the delay and the win is on secondary quality.

**Silence where the rule does not apply.** 34 control cases across three runs, and in not one
of them does the arm with the rule add a remark about when to send. The deliberate baits in
runs 02 and 03, a CI workflow full of clock words, a SQL window of 90 days and a subject line
naming a billing date, never triggered it.

Both of those are established by reading all the answers, which is why they survive a noise
floor that destroys the verdict counts.

## What does not hold

**The prescribing clauses.** Two violations reproduce across runs 02 and 03 in the arm that
carries the rule, and both were verified in the answer files rather than taken from a judge's
summary:

- `own-user-overrode-advice`: the drafted email drops the user's own clause "and I'm not
  signing up for that" in both runs. In run 03 the answer additionally states "the rest is
  unchanged", which is false. Message integrity is the axis the rule exists to protect.
- `own-after-choice-supplier`: the draft keeps a patronymic that the planted history had
  invented and loses the surname the user did give, in both runs, while the arm without the
  rule keeps both.

`own-resignation-flat` improved: run 02's "send it now and don't think about it again" became
a two sided line ending "her working hours are unknown to me, so that's yours to judge".
Run 03's gates recorded in advance that a flip on this case is to be read as variance, and
with 9 of 12 null cases flipping between the same two runs that reading stands.

**The length effect is noisier than runs 01 and 02 reported.** Median characters outside the
drafted message rose where the rule applies after the edit, `ownership` 150 to 326 and
`timing-now` 222 to 294, which is what the edit was for. But on controls the arm without the
rule moved from 536 to 341 on byte-identical input, a 36 percent swing from sampling alone,
so the earlier reports quoted this measure more confidently than it deserved.

## Conclusion

Do not propose a built-in rule. Across three runs, 146 answers and 146 verdicts, the
behaviour the rule was written to produce is not established, two of its own clauses are
violated reproducibly by the arm that carries it, and the only robust effects are a carve-out
that prevents a harm the baseline does not commit anyway and silence where the rule does not
apply. An always-on directive costing 942 to 1,029 characters on every turn and in every
compact worker package has to buy more than that.

What survives the trial, unchanged from the earlier recommendation:

- **The paired skill**, where the method has room and loads only for roles that prepare
  correspondence. Nothing measured here argues against it.
- **The delegation placement**: a worker that prepares or verifies outbound text gets the
  delivery moment as an acceptance criterion from its parent, at no always-on cost.

If the question is reopened, the instrument has to change before the rule does. At 8 to 12
cases per group the per-group net is unusable; a usable version needs several runs per arm
with the same contexts and a null group reported as the noise floor, which is what the
control group turned out to be measuring here.

## Harness defects found along the way

Worth filing separately from the rule question:

- `answers.js` and `judges.js` give the agent an absolute path, and some agents resolve it
  against their working directory instead: 19 of 52 answers in run 02 and 5 of 52 in run 03
  landed in a sibling directory, and one run 03 agent wrote a file literally named
  `placeholder.md`. Every file was recovered and matched one for one against the plan before
  judging.
- In both cases the workflow reported every agent done with no errors, because the script
  cannot tell a written file from a reply of "done". The loss would not have been silent:
  `build_judges.py` checks that both answers of a pair exist and are non-empty, records the
  gaps in `judge_skipped.json` and exits non-zero unless `--allow-partial`, and
  `aggregate.py` reports them as missing on the same terms. So the cost is a misleading
  completion report plus a manual recovery step, not judged data that was never there.
- Two judges wrote trailing text after their JSON object, so `aggregate.py` counted the
  verdict as missing. Both were re-judged rather than edited.

## Limitations

- One answer model in all three runs, judged by the same model family.
- 6 to 8 cases carry the headline claim in each run, which the noise floor above shows is
  too few.
- Run 03 changed the rule text, so its `without` arm cuts 1,029 characters against 942 in
  the earlier runs. That confound was recorded before the run.
- The explanation offered for run 01's result, that its rubrics rewarded the rule's shape,
  is an interpretation of three runs rather than a fourth measurement.
