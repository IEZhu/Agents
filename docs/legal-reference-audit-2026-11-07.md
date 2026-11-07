# Legal reference audit, 2026-11-07

> Recorded result. It retains its original scope, measurements and limitations;
> the procedure it ran is [the legal reference refresh flow](../flows/legal-reference-refresh.md),
> which step 8 of that flow now prescribes for every refresh.

This is the audit of the labour-law layer added for eight jurisdictions plus US
investment law. It verified the files **as committed**, because the round before
it had verified the drafts and then put 145 corrections in by hand with nothing
checking them.

The per-claim ledgers — 718 rows across nine jurisdictions — belong to the run
that produced them and are not committed: they are working evidence, superseded
at the next refresh. What is recorded here is what a later reader needs and
cannot reconstruct: the method, the counts, the decision rule and how it fell,
the limits, and the errors the audit made about itself.

Protocol fixed in advance in the run's pre-registration file, before any worker ran.
The sampling frame, the seed, the strata, the thresholds and the decision rule
could not be changed by what came back, and the two deviations are recorded as
deviations.

## Worker accounting

24 workers in this audit, all prepared through the delegation contract and all
validated before being read as evidence:

| Line | Workers | Valid | Rejected |
|---|---|---|---|
| Blind replication, one per jurisdiction | 9 + 1 re-attempt | 9 | 1 (Colombia, attempt 1) |
| Traceability, three files each | 3 | 3 | 0 |
| Positive control, seeded copies | 2 | 2 | 0 |
| Cross-file consistency | 1 | 1 | 0 |
| Unsupported statements, one per file | 8 | see below | see below |

The one rejection was a new envelope defect: a `checks` entry referenced
`evidence_ids` the worker never defined, so the result was discarded under
`unknown_evidence_id`. Its research was not read and not integrated; a fresh
blind line ran instead, with that rule written into its package.

## C — the method's own sensitivity: 12 of 12

Both control lines found **every** seeded defect in their file, across all six
classes: a stale figure, a wrong subsection, a reversed negation, a substituted
deadline, a repealed instrument, and a wrong base of computation. One-sided 95%
lower bound on sensitivity: **77.9%**.

The Spanish line also caught something not seeded: the BOE identifier invented
for the repealed decree belongs to a different document. A fabricated locator
was detected as a fabricated locator.

Because the method demonstrably detects defects of exactly these classes, the
null findings of the other checks carry evidential weight. Without this number
"no errors found" would have meant nothing.

The control lines also returned **eleven defects that were never seeded**, in
the real text: five in the Spanish file and six in the United States file. All
eleven were integrated. One of them — *Pollard* escaping the cap at
1981a(b)(3) rather than (b)(2) — was found independently by the traceability
line, two different routes reaching the same conclusion.

## B — blind independent replication: 83 of 90

90 pre-registered assertions, ten per file, verified by nine lines that were
given the committed file and the sampled items and **nothing else**: no ledger,
no findings, no verdicts. 83 confirmed, 6 refuted, 1 unverifiable.

**Every item in the mandatory stratum was confirmed independently** — all nine
of the high-impact corrections the pull request leads with, including the
contested negated chain of art. 104(1-1) of the Kazakh Labour Code, which a
line that had never seen round one read the same way.

The single unverifiable item **vindicated our own hedge**: the Serbian 2027
minimum wage is served by no reachable publisher, so the file's existing
`(unconfirmed)` marker is right and stays.

The six refutations, by consequence:

- **Changes the answer** (2): Mexico's Juntas are not gone, so the forum named
  for a pre-transition case was wrong; Serbia's čl. 183 does not reach a
  probation dismissal, which the file said it did.
- **Misstates which rule is qualified** (1): Colombia's *conditionally
  exequible* marking sat on the wrong provision.
- **Citation defect, substance intact** (2): a dead DOF date, and a Georgian
  article credited with day limits it does not carry.
- **Our hedge was wrong, not the law** (1): the European Committee of Social
  Rights decisions are reachable after all.

Exact binomial one-sided 95% upper bounds on the residual error rate:

| Counting | Errors in 90 | Upper bound |
|---|---|---|
| All six refutations | 6 | 12.73% |
| The three that misstate the law | 3 | 8.39% |
| The two that change the answer | 2 | 6.83% |

Cohen's kappa is not reported, and deliberately: with agreement this close to
ceiling and a base rate this skewed, kappa is degenerate and would say less
than the bound above. The informative statistic for an audit is the exact
confidence bound on the error rate, not an agreement coefficient.

## A — traceability of what shipped: 1,242 statements classified

Three lines classified every figure, deadline, article number, rate, threshold
and named instrument in the nine committed files against the records it was
built from.

- **22 contradictions** between a shipped statement and its own record. All 22
  were assessed; the corrections are in the commit.
- **73 statements supported by nothing verified** — mostly pin-cites to
  articles whose content no record establishes. The substance may be right; the
  support was missing. Eight further lines were run on exactly these.
- **205 statements traceable only to an accepted line's detail file** rather
  than to its envelope. That is the expected state, not a defect: the envelope
  is capped at ten evidence entries and fourteen findings by design, and the
  detail file is where the substance lives.

## E — cross-file consistency

No cross-file contradiction of fact. The Brussels I bis, Rome I, ECHR and
European Social Charter article numbers agree everywhere they appear. Fourteen
findings, all wording-strength or coverage differences, plus one real internal
contradiction in the Spanish file, which named three different things as the
first step and now names one.

## D — the guard was observed firing: 6 of 6 mutants

Reported in the run's parent-side record. The decisive mutant is the one where
the rolling window is still open and the pinned calendar date has passed: the
suite fails, which is the whole reason `review_on` exists. Seven unit cases now
hold that arithmetic permanently, because every file in `skills/` sits inside
both windows and never reaches that branch.

## The decision rule, and that it was not met

Fixed in advance, and reported as it fell:

| Condition | Threshold | Result |
|---|---|---|
| Substantive refutations in the sample | ≤ 2 | **failed** — 3 misstate the law, 5 touch a figure, deadline or citation |
| Refutations in the mandatory stratum | 0 | met — all nine confirmed independently |
| Refutations in the catastrophic class | 0 | met — no limitation period, challenge deadline or non-waivability rule was refuted |
| Orphan statements asserting a number | 0 | **failed** — 73 |
| Method sensitivity | ≥ 9/12 | met — 12/12 |
| Guard mutants behaving as specified | all | met — 6/6 |

Four of six conditions met, two failed. Every failing item has been corrected
or is in the hands of a verification line; the thresholds were not moved to fit
the result, and the two failures are the honest headline of this audit.

## Deviations from the pre-registration

1. Six guard mutants were run where four were specified: the two extra cover a
   missing `sources` list and a `review_on` equal to `as_of`. A superset.
2. Two checks were added that the pre-registration does not contain: a tally of
   every claims ledger against the figures the pull request reports, and a
   liveness probe of all 75 declared source URLs. Both are recorded in
   the run's own records as additions,
   not as planned checks.

## One error of my own

The package for the Georgia–Kazakhstan–Serbia traceability line asserted that
no accepted verification result existed for Kazakhstan. One does:
`kz_refute_4` validated. On the correct premise two of that file's four
reported contradictions dissolve, one was a real defect and is fixed, and one
rests on a rejected line and was correctly never integrated. The line's work
was sound; my premise was not.

## The 73 unsupported statements, verified

Eight further lines, one per file, each limited to its own list and each
validated before being read. All eight valid.

| File | Items | Confirmed | Refuted | Unverifiable | Other |
|---|---|---|---|---|---|
| ge | 19 | 17 | 1 | 0 | 1 partly overstated |
| inv-us | 14 | 13 | 1 | 0 | — |
| co | 9 | 6 | 2 | 1 | — |
| cy | 8 | 6 | 2 | 0 | — |
| es | 8 | 7 | 1 | 0 | — |
| mx | 7 | 6 | 1 | 0 | — |
| us | 5 | 5 | 0 | 0 | — |
| kz + rs | 3 | 3 | 0 | 0 | 1 precision point |
| **total** | **73** | **63** | **8** | **1** | **2** |

So the class was about 86% right and wholly unsupported, and it hid eight real
errors — including one that dropped the money a constitutional court awards.
That is the argument for the check: a statement can be correct and still be a
defect, because nothing distinguishes it from the eight that were not.

Of the eight, the ones that change what a reader would do: Colombia's
*estabilidad reforzada* remedy is cumulative and includes 180 days' indemnity,
not "void, not compensable"; Cyprus does have a public archive of collective
agreements, and a residence permit is revoked by a reasoned, challengeable
decision rather than lapsing automatically; Spain's *nulo* dismissal is not
"no indemnity"; and CFIUS finality can come out of a declaration, not only a
completed notice.

One declared source was dropped rather than corrected: the Colombian
SUIN-Juriscol URL answers every document id, valid or not, with a
byte-identical single-page shell, so it never served the document it was cited
for.

## Totals

49 corrections integrated by this audit, on top of the 145 of the first round.

24 workers ran in the audit: 9 blind replication plus 1 re-attempt, 3
traceability, 2 positive control, 1 consistency and 8 on the unsupported
statements. 23 of 24 envelopes validated. The one rejection was re-run from
scratch and its content was never read.

Across both rounds: 52 workers, 39 valid envelopes and 13 rejected, every
rejection on envelope formatting and not one of them integrated.

## The audit's own checks, committed

Two of the checks this audit improvised are now part of the repository, so the
next refresh does not have to invent them.

`tests/test_skill_invariants.py` — 118 cases over the 18 volatile skills,
offline and deterministic, so CI enforces them: every source over `https`, no
placeholder in prose, no invisible character anywhere, no word mixing Latin and
Cyrillic, balanced emphasis, square tables, a future date that says it is not
yet in force, and no literal keyword shared with a sibling skill for the same
country.

It was mutation-tested before being committed, to the same standard the
freshness guard was held to. Seven mutants, one per defect class, each applied
to a copy of one skill with the tree restored after every run:

| Mutant | Expected | Observed |
|---|---|---|
| a source over plain `http` | fail | fails the https case |
| `TODO` in prose | fail | fails the placeholder case |
| a word mixing Latin and Cyrillic | fail | fails the mixed-script case |
| an unbalanced `**` | fail | fails the emphasis case |
| a ragged table row | fail | fails the table case |
| a future date stated as current law | fail | fails the future-date case |
| a keyword copied from the sibling skill | fail | fails the pair case |

The first run caught two defects in the check itself, which is the argument for
running it. The future-date pattern admitted a bare year as its own
not-yet-in-force marker, and a line stating a 2027 date always contains "2027",
so that case could never fail — it is words only now. And the placeholder rule
flagged `Sentencia T-XXX/YY`, a citation-format template inside backticks, so
the rule now reads prose and leaves code spans alone. Both are the same lesson
as the double-space rule earlier: a check that cries wolf is a defect in the
audit, not in the file.

`scripts/check_skill_sources.py` — the network side, deliberately not a test
because the suite runs without a network. It reads the body rather than
trusting the status, retries once before reporting, and sends the `Accept`
header an API documents. It fails only on a URL that is positively wrong, a 404
or an error page under a 200; a host that refuses the client is reported and
does not fail the run, because that is a limitation of where the check ran.
Its docstring says plainly that the client is not to be disguised to get past
such a block.
