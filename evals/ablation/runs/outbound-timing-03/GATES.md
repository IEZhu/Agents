# Pass gates and predictions for rule-outbound-timing, run outbound-timing-03

Written before the contexts were built and before any answer existed.

## What changed, and only that

The case set is byte-identical to run 02: both `cases/rule-outbound-timing.json` files
hash to `ff83b971`. The rule text changed in one clause and nothing else.

Before: `Hand over the draft, then at most one line: when it lands, ...`
After: `Hand over the draft, then one line on the moment, no more: when it lands, ... That cap
is on the timing line alone, not on what else the draft needs.`

The file goes from 1,009 to 1,096 bytes and the rendered rules block from 11,091 to 11,178
characters, so the `without` arm now cuts 1,029 characters rather than run 02's 942. That
is the one confound this run carries and it is recorded here rather than discovered later.

Because the cases are identical, run 03 against run 02 is a per-case comparison, which is
what neither of the first two runs could do.

## Why the change

Run 02's `ownership` group failed at net -2 with 3 robust wins against 4 robust losses.
Reading the four losses: one was the arm with the rule keeping a fabrication the baseline
caught and corrected, which is a cost of compressing the note; one was a case-design
tension declared in advance; and two were the arm with the rule breaking a clause the rule
itself states. Across both runs the arm with the rule wrote roughly a third the commentary
wherever the rule applied, in all 14 applicable cases, which is the mechanism proposed for
the first of those losses. The revised clause scopes the cap to the timing line only.

## Gates, unchanged from run 02 so the goalposts do not move

1. **`ownership`**: `net` above 0, `robust with` at least 5 of 8, `robust without` 0.
2. **`timing-now`**: (a) in none of the 6 cases does the arm with the rule advise waiting,
   established by reading; (b) `robust without` at most 1, and any such loss must be
   attributable by the judge's own reasons to something other than the delivery moment.
3. **`control`**: (a) in none of the 12 cases does the arm with the rule add a remark about
   when to send, established by reading; (b) the control sign test does not reach p below
   0.05 in either direction.

## Predictions, registered so the result can falsify them

- **P1** The three run-02 losses other than the two clause violations change character:
  `own-after-choice-supplier`, where compression is the proposed mechanism, flips or ties.
- **P2** The two genuine clause violations are **not** fixed by this change, because neither
  turned on the cap. `own-resignation-flat` issued a conditional send-now instruction and
  `own-user-overrode-advice` dropped a clause from the user's draft. If either flips, that is
  not this change working; treat it as run-to-run variance and say so.
- **P3** Commentary where the rule applies rises toward the baseline: the `ownership` median
  with the rule was 150 characters against 470 without, and `timing-now` 222 against 920.
  Both should move up.
- **P4** The controls do not move: median commentary stayed near the baseline there in run 02
  (368 against 536, shorter in 6 of 12), and should stay so.
- **P5** Gates 2 and 3 still pass. The send-now carve-out and the silence on controls held in
  both previous runs and the change does not touch either clause.

If gate 1 passes while P2 holds, the pass rests on two or three cases and is weak. If gate 1
passes because the two clause violations flipped, the honest reading is variance, not effect.

## Standing limitation

A third run on the same 26 cases with the same answer model cannot separate a real
improvement from regression to the mean on the four cases that lost last time. A pass here
is a reason to keep testing, not a result.
