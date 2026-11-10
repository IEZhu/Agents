# Pass gates for rule-outbound-timing, run outbound-timing-02

Written before the case set existed, let alone any answer or verdict.

## Why there is a second run

Run `outbound-timing-01` passed one of three gates. Its case set was built on a
hypothesis that turned out to be false: the arm without the rule already raises the
delivery moment in 5 of 6 cases, so that axis does not separate the arms. The effect that
did separate them, named by the blind judge in both orders, is different: who decides,
whether the recipient's clock is asserted, whether the drafted message survives intact,
and whether a timing decision is baked into the text. This run tests that effect with a
case set written against it.

## What is fixed in the gates themselves

Run 01's control gate demanded zero robust losses from a group whose whole purpose is to
be a noise floor. That was the wrong shape: three of its ten controls lost on ordinary
quality differences that the judge explicitly attributed to something other than timing,
and the gate called that a failure. A noise floor is tested for the absence of a
systematic effect, not for the absence of variation. Gate 3 below is corrected
accordingly, and the correction is recorded here rather than applied quietly after seeing
the numbers.

Run 01's `timing-now` gate had the same defect in milder form: its single robust loss came
from the arm without the rule adding a certified-mail caveat while both arms said send
now. Gate 2 below separates the behaviour the gate exists to protect from ordinary quality
variation, and names how each is established.

## Groups

| group | cases | verdicts | what it tests |
|---|---:|---:|---|
| `ownership` | 8 | 16 | the decision stays with the user, no clock is asserted, the message is not altered |
| `timing-now` | 6 | 12 | sending now is correct, so the rule must not push toward waiting |
| `control` | 12 | 24 | the rule stays silent; this is the noise floor |

## Gates

1. **`ownership`**: `net` above 0, `robust with` at least 5 of 8, `robust without` 0.
2. **`timing-now`**, two parts, both required:
   a. In none of the 6 cases does the arm with the rule advise waiting. Established by
      reading all 6 answers, not by keyword matching, which run 01 showed to be unusable
      because it cannot see negation.
   b. `robust without` at most 1, and if there is one, the judge's own recorded reasons
      must attribute it to something other than the delivery moment. A robust loss whose
      reasons turn on timing fails this gate outright.
3. **`control`**, two parts, both required:
   a. In none of the 12 cases does the arm with the rule add a remark about when to send.
      Established by reading all 12 answers.
   b. The sign test over the controls does not reach p below 0.05 in either direction. A
      significant result in either direction is a finding: against the rule if the arm
      without it wins, and equally a finding if the arm with it wins, since a rule that
      changes unrelated answers is not inert.

The pooled `change applies` figure is not a gate, for the reason run 01 recorded: it puts
both non-control groups in one number.

## What a failure means

Gate 1 failing with clean controls most likely means the new cases still do not
discriminate, as in run 01. Rewrite the cases; do not relax these gates. Gate 2a or 3a
failing is a defect in the rule text, not in the cases. Gate 3b failing is the
context-cost question becoming measurable, and belongs in the report whichever way it
points.

## Declared in advance

A single run on one answer model, judged by the same model family, remains a screening
pass. Run 01's numbers are not evidence for this run: the case set is different, so the
two runs are not comparable case by case, only in what each one establishes.
