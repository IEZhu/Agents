---
persona:
  agent: lawyer
  skills: [skill-legal-citation, skill-fact-verification, skill-source-trust-tiers, skill-temporal-validation]
  implants: [implant-chain-of-verification]
---
# Re-verify and re-date a legal reference skill

This is an executable instruction for an AI. It takes one or more
`skill-jurisdiction-*`, `skill-labor-*` or `skill-investment-*` skills and brings
their figures, article numbers and procedural deadlines back to a verified state,
then moves their `as_of` and `review_on` dates forward.

It exists because these skills fail silently. They are injected into the system
prompt, so the model reads a stale minimum wage or a repealed article as
authoritative context, and `rule-no-fabrication` does not catch it: the figure is
in context, not recalled. A 2026-09 audit found outdated figures in 7 of the 9
jurisdiction skills, and the 2026-11 build of the labour skills found that the
earlier failures were not wrong numbers but **missing layers** — a whole
collective-agreement or probation layer absent, so every answer built on the file
was incomplete rather than incorrect.

The result is a branch with the refreshed skills, a per-claim verification record,
an explicit limits section and a pull request against `dev`.

## How to invoke

```text
Run flows/legal-reference-refresh.md for skill-labor-es.
Run flows/legal-reference-refresh.md for every skill whose freshness test fails.
Run flows/legal-reference-refresh.md for skill-jurisdiction-cy and skill-jurisdiction-kz.
```

## Inputs and authority

| Input | Default |
|---|---|
| Skills | Required: the skill IDs, or "every failing skill" — then take them from `tests/test_skill_freshness.py` |
| Scope per skill | Every figure, article number, deadline, threshold and institution name in the body, plus the layers in the completeness checklist below |
| Branch | `claude/legal-refresh-<slug>` or `codex/legal-refresh-<slug>`, cut from `origin/dev` with `--no-track` in a worktree, never in the live installation checkout ([AGENTS.md](../AGENTS.md#make-changes)) |
| Lines | One prepared worker per skill for research, one per skill for refutation, one gap critic across all of them |
| Dates | `as_of` is the date the research ran. `review_on` is the next calendar reset; see "Dating the result" |

The owner decides: dropping a jurisdiction, lowering the primary-source
requirement, and shipping a skill whose core figures stayed unconfirmed. Decide
everything else from this flow.

## Why this procedure and not a single pass

One model reading one source and writing the file is the arrangement that produced
every past failure. The procedure below is built to make an error survive fewer
steps, and each element removes one specific failure mode:

| Element | Failure mode it removes |
|---|---|
| One worker per jurisdiction, clean context, no shared conclusions | Correlated error: one wrong premise propagating into every country's file |
| Primary sources only for load-bearing facts | Aggregator drift: a figure copied between blogs and aged in all of them |
| Two independent retrieval paths per figure | A single mis-parsed page or an outdated consolidation read as confirmation |
| A separate refutation line, instructed to refute, that defaults to "refuted" when uncertain | Confirmation bias: a verifier asked "is this right?" agrees |
| A gap critic that is asked only what is missing | The actual 2026 failure: a complete-looking file with an absent layer |
| An explicit limits section, never a silent drop | Unverifiable claims quietly becoming assertions |
| `validate_subagent_result` before reading any result as evidence | Accepting output that does not even match its contract |
| A dated freshness contract enforced by a test | Everything above decaying again without anyone noticing |
| A post-integration audit of the file that actually shipped | The integrator's own edits: 145 corrections typed by hand after every line had finished |
| Seeded defects in a control copy, to measure the method's own sensitivity | "No errors found" being read as "no errors present" |

None of this makes a claim true. It makes an unsupported claim visible.

## Delegated workers

Follow the [mandatory delegation contract](../docs/routing_flow.md#delegated-workers).
For each line call `get_subagent_context` and require `status: prepared`; start a
fresh native worker with the receipt's `launch` text; keep the `contract_revision`;
call `validate_subagent_result` with that revision and `result_ref` for every
returned or revised output; require `valid: true` before reading the result as
evidence, and then judge the evidence yourself. Structural validity is not
correctness, and a `partial` result with named gaps is a usable result — an
invented complete one is not.

Record each artifact's validation outcome beside it — the attempt id, `valid` or
not, and where the accepted result was filed. A directory of results is not a
record of acceptance: on the 2026-11 run the accepted Kazakhstan result was
never copied into the accepted-results folder, and its absence read for a while
as a rejection that had not happened.

Give each worker one slice. Do not let a research worker verify its own output:
the refutation line must not know what the research line concluded beyond the
claim it is asked to break.

## Steps

### 1. Establish the baseline

Read each target skill in full and record, as a list, every load-bearing specific
it currently states: figures, article numbers, deadlines, thresholds, institution
names, and the `as_of`/`review_after_days`/`review_on`/`sources` frontmatter. This
list is what the research and refutation lines are measured against. Note which
claims already carry an `(unconfirmed)` marker — those are not regressions.

### 2. Research lines, one per skill

Prepare one `lawyer` worker per skill with `result_type: research` and
`complexity: high`. Each assignment must demand:

- the body in the skill's exact section template, so the files stay parallel;
- a norm for every rule statement, as statute abbreviation plus article;
- a year or effective date on every number;
- a claims ledger: `id | claim | norm cited | source URL | confirmed this session
  (yes/no) | how confirmed or why not`, one row per load-bearing specific;
- a minimum count of rows citing an official primary source — the national
  gazette, the legislature, the labour, tax or social-security authority, the
  court registry, EUR-Lex for an EU instrument — with aggregators and law-firm
  blogs explicitly not counting toward it;
- two independent retrieval paths behind every row marked confirmed, both named;
- `(unconfirmed)` kept in the body for anything that could not be confirmed,
  never dropped and never replaced by a guess;
- a limits-of-verification report: blocked hosts, 403s, captchas, paywalls,
  translation-only sources, documents whose full text would not open.

### 3. Completeness checklist

Each refreshed labour skill must cover these layers, and say so explicitly where
one has no local equivalent rather than omitting it. They are the layers whose
absence caused the recorded failures:

1. The collective layer — the applicable collective agreement, how it is
   identified in practice, and that it can impose duties the statute alone does
   not.
2. Probation, which while it runs can let an employer end the contract without
   cause or indemnity and therefore dominates every other argument.
3. Non-waivable rights: the provision that voids an advance waiver, so that a
   contractual "no right to compensation" clause cannot work.
4. Exclusivity, moonlighting and non-compete, separating a statutory duty of
   loyalty from a contractual full-dedication clause and from a post-termination
   restraint, each with its compensation condition.
5. The employment-to-immigration coupling, where a sponsored permit depends on the
   job, so that a dismissal is also a status event.
6. Spillover into tax and social security when a worker takes paid side activity,
   including loss of a special tax regime.
7. The personal scope of constitutional labour rights, which in several
   constitutions is drafted for nationals, so a foreign worker's anchor is the
   labour statute and the immigration law instead.
8. Dispute procedure as arithmetic, not narrative: the mandatory pre-litigation
   step, each limitation period with what suspends it, who bears the burden of
   proof, the distinct dismissal outcomes with their distinct consequences, the
   severance formulas with their caps, and the mandatory forum rules that defeat a
   foreign exclusive-jurisdiction clause.

For a `skill-investment-*` skill, substitute its own layer list: what counts as a
regulated instrument, registration and its exemptions, investor eligibility
thresholds, resale restrictions, intermediary licensing, sub-national
registration, foreign-investment screening, and the enforcement and private-action
channels.

### 4. Refutation lines, one per skill

Prepare one `investigative_analyst` worker per skill with `result_type: review`.
Give it the drafted body and the claims ledger — pin them as
[artifacts](../docs/shared-artifacts.md) when the workspace allows, so the
verifier reads the exact bytes — and instruct it to **refute**, not to confirm:

- open the cited primary source for each load-bearing row and check the article
  number, the operative wording and the current consolidation date;
- flag a truncated quotation whose full operative sentence changes the weight of
  the argument it supports, which is a distinct defect from a wrong number;
- flag a claim whose cited source is an internal administrative criterion, a
  guidance note or a ministry leaflet presented at the same level as a statute,
  and name its actual legal force;
- flag a figure whose two "independent" paths turn out to be the same source
  twice, or an unofficial copy of an official document;
- flag a case citation whose full text was never opened, and say so, rather than
  treating a search-result summary as the holding;
- where appellate practice diverges, require the file to report a split rather
  than a rule;
- default to `refuted` when the source cannot be reached, and say which host
  blocked it.

A refutation line's job is done well when it returns findings. A clean refutation
pass over thirty claims is a reason to check the line itself.

### 5. Gap critic

Prepare one `black_hole_finder` worker across all refreshed drafts, asked only
what is missing: a layer from the checklist that is formally present but hollow, a
jurisdiction-specific mechanism nobody mentioned, a question the file lets a reader
answer in general terms. Accept a `partial` result and carry its open questions
into the limits section.

### 6. Integrate

Apply the refutation findings and the gap critic's additions yourself; do not ask a
worker to edit its own file. For each claim, the body ends in exactly one of three
states, and none of them is silence:

- **stated with its norm** — confirmed by two paths, both in the record;
- **stated with `(unconfirmed)`** and a one-clause reason;
- **removed**, with the removal named in the pull request.

Keep the house format: quoted `description` and `compiled` values, `keywords` that
do not collide with the neighbouring country skill's, local-language legal terms
with an English gloss, English prose everywhere else.

### 7. Date the result

- `as_of`: the date the research lines ran. Not the date of the merge, and never a
  date no line actually worked on.
- `review_after_days`: the rolling window. 180 is the default.
- `review_on`: the next calendar date on which the figures are known to reset —
  for most employment and tax figures the weeks after 1 January, once the
  implementing decree is published. Set it whenever a figure in the file has an
  annual reset, because the rolling window alone would keep serving last year's
  number for months.
- `sources`: every source actually used, official ones first.

`tests/test_skill_freshness.py` enforces the earlier of the two dates. A skill
that cannot be verified now gets an honest short `review_on`, not a long one.

### 8. Audit what shipped

This runs before the pull request is opened, not after it. Steps 2 to 5 verify
the **drafts**. Step 6 then puts the corrections in by hand,
and nothing has checked that. This step audits the file as committed. Write the
protocol down **before** running any of it — sampling frame, seed, strata,
thresholds and the decision rule — in a file the results cannot change, and
record any later deviation as a deviation.

**Traceability.** One worker per three files, no network. Classify every figure,
deadline, article number and named instrument in the shipped file as supported by
a confirmed ledger row, supported by an accepted finding, raised by the gap
critic, present only in a detail file whose envelope was rejected, orphaned, or
contradicting its own record. An orphan that states a number is a defect of the
integration step whether or not it happens to be true.

**Blind replication.** One worker per jurisdiction, each given the committed file
and a pre-registered sample of about ten assertions, and **nothing else** — no
ledger, no findings, no verdicts. It must be able to disagree. Draw the sample
from every body line carrying a numeral or a citation, stratified: the round's
own high-impact corrections first, as a mandatory stratum, then at least two
money lines and two deadline lines, then random fill. Report the exact binomial
one-sided 95% upper bound on the residual error rate; at zero errors in ninety
items that bound is about 3.3%, which is the honest ceiling, not a clean bill.

**Positive control.** Seed six plausible defects into a copy of one or two files
whose publishers are reachable, of the same classes the real defects were — a
stale figure, a wrong subsection, a reversed negation, a merged deadline, a
repealed instrument, a wrong multiplicand — record them before starting, and set
a refutation line on the copy without telling it anything was seeded.
Sensitivity is detected over seeded. Below three quarters, the clean results of
the other checks carry little weight and the report says so instead of reporting
them as clean.

**Mutate the guard.** A guard is only a guard if it fails when it should. Against
a copy of one volatile skill, with the tree restored after each run: `as_of`
moved past the rolling window must fail; `review_on` set to a past date while the
rolling window is still open must fail, which is the whole reason `review_on`
exists; `review_on` removed with `as_of` fresh must pass; `as_of` removed must
fail; `sources` removed must fail; `review_on` equal to `as_of` must fail. The
unit cases at the end of `tests/test_skill_freshness.py` hold the date
arithmetic permanently, because the files in `skills/` are all inside both
windows and never exercise that branch.

**Cross-file consistency.** One worker over all the files at once. Two of our own
files disagreeing on a supranational rule is a defect even where neither is
refuted alone.

**Mechanical invariants**, in the parent, no worker: every source `https` where
the host serves it, no placeholder text, no mixed-script words, no zero-width or
non-breaking spaces, balanced emphasis, no ragged tables, zero keyword
collisions with each neighbouring country skill, every future date marked as not
yet in force, and the count of `(unconfirmed)` markers that ship. Report a check
that found nothing to examine as vacuous rather than as passed.

**Decision rule.** Fix it in advance and let it be able to fail: a ceiling on
substantive refutations in the sample, zero in the mandatory stratum, zero in the
catastrophic class — a limitation period, a challenge deadline, a non-waivability
rule — zero orphans that state a number, the sensitivity floor, and every mutant
behaving as specified. Failing the rule does not mean discarding the work. It
means the pull request names the failing check and does not claim the conclusions
are verified.

### 9. Checks and hand-over

Run, from the worktree:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_skill_freshness.py -q
```

```bash
.venv/bin/python -B scripts/validate_agents.py
```

```bash
.venv/bin/python -B scripts/check_docs.py
```

Then open the pull request against `dev`. Its description states, per skill: how
many claims were checked, how many are confirmed by two paths, how many remain
`(unconfirmed)` and why, what the refutation line changed, and the new `as_of` and
`review_on`. Report the number of workers that ran and what each was for. Leave
the request unmerged for the owner.

## Report

Write the report in the language of the invocation. It contains:

1. Per skill: claims checked, confirmed, unconfirmed, removed; the new dates.
2. What the refutation lines actually changed, as findings rather than a count.
3. What the gap critic added.
4. **Limits of verification** — every blocked host, captcha, paywall,
   translation-only source and unopened full text, and every claim still
   unconfirmed. This section is not optional and is never empty in practice.
5. The post-integration audit: the sample size and how it was drawn, the error-rate
   upper bound, the measured sensitivity of the method, the orphan count, and every
   mutant's behaviour. A bound and a sensitivity figure, not an adjective.
6. The branch, the pull request and the commands that were run.

A refresh that reports no limits has not looked hard enough at its own evidence.
