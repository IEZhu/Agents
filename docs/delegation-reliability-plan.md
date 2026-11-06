# Reliability of delegated cooperation

Status: proposal, 2026-11-06. Not implemented.
Builds on [the task-scoped subagent contract](subagent-contract.md) (#291) and the
peer-delegation epic #323 (`agents-core/subagent/2`, #324 to #330).
Related: #534 (what a failed check means), #547 (a check that reuses the claim's
own route), #563 (an independent check of declined findings), #567 (a
planted-defect benchmark for reviewers), #443 (tier evaluation).

## Problem

The exchange tells a parent whether a worker's output has the right shape. It
tells it nothing about whether the output is true, and it offers no vocabulary
for saying how sure the parent is after accepting it.

`validate_subagent_result` returns `verification: structural_only`, and the
contract document states plainly that a valid envelope "does not establish the
truth of the completion claim". That honesty is correct and must stay. The gap
is what comes after it: the protocol hands the parent an unstructured
instruction — "independently assess its evidence, actual checks, limitations and
acceptance criteria" — and no mechanism to carry out, record or measure that
assessment.

In practice the parent therefore does one of three things, none of them
measurable:

- accepts the result because it is well written and structurally valid;
- spawns another worker to look at it, and accepts when the second agrees;
- re-does the work itself, which defeats the delegation.

The second is the one the evidence condemns most directly, and the measurement
now exists for this exact setting: 48 implementations of one specification,
produced by 5 different coding-agent harnesses across 23 models and 3 languages
and run against a million test inputs, failed together on 429 inputs where
independence predicts 115.36 — z = 29.20 — and the authors' own answer to their
research question is that cross-language and cross-agent generation "do not
provide enough diversity to make failures independent". Nothing in the contract
or the result records what made a second look different from the first, so the
protocol cannot distinguish a genuine independent check from a restatement.

The same paper refuses the lazy conclusion, and so must this plan: majority
voting over three-version units still cut the mean failure count from 387.44 to
130.99, about 66 %, with 11,844 three-version units showing no observed failure
at all. Redundancy across agents is therefore not worthless; it is simply not
independent. Its benefit has to be measured rather than inferred from an
independence assumption that is known to be false.

This plan proposes the smallest set of changes that turn "the parent assesses the
evidence" into a procedure with a recorded answer and a measurable error rate,
adds a place for a validated fact to live once it has been established, and makes
each mechanism subject to being cut if the measurement says it is ceremony.

## What the exchange already guarantees

These are load-bearing and the plan keeps all of them.

| Guarantee | Where it comes from |
|---|---|
| The assignment is fixed before the work starts, and changing it requires a new attempt | `contract_revision` is the SHA-256 of the canonical contract, including `task_id` and `attempt_id` |
| A result cannot be replayed against another task, attempt or contract | identity checks in `src/engine/subagent_validation.py` |
| Inputs can be pinned exactly | `artifact_manifest` hashes bound into the contract; `artifact_manifest_verify` for the parent's separate check |
| A claim must point at something | `evidence[]` with stable IDs; findings, conclusions and checks may only reference supplied IDs |
| A worker must not present an unrun check as run | `rules/subagents/rule-worker-result.mdc`: checks are `passed`/`failed`/`not_run` "from execution, never suggestions" |
| A worker must not silently fill a gap in its input | `input_required` with a concrete question; `partial` with `remaining_work` |
| Validation never dereferences anything a worker wrote | `verification: structural_only`; no path or URI is opened except the stored contract and `result_ref` |
| Preparation and validation hold no state | stateless server; the parent's working set is the state |
| A cheaper model runs only where the parent rated the work cheap, and a shortfall climbs a tier | `complexity` → `execution.tier`; `escalation()` in `src/engine/model_tiers.py` |

The #323 epic adds the dialectical half: a worker may object to a named part of
its contract, decline it, propose a reroute or address the user, and the speaker
owes a disposition for each. #327 already specifies its evaluation in the right
form — planted defects, clean tasks, adversarial inputs, blind judging, ceremony
detectors with a cut rule stated before the run.

## What is deliberately absent, and what is simply missing

Deliberate, and correct:

- Validation asserts nothing about truth.
- The server holds no delegation ledger with refusal semantics (#323, decision 3:
  on a stateless server a stale ledger is indistinguishable from a current one).
- No worker flag has an automatic effect; everything is advisory and rejectable.

Missing, and the subject of this plan:

1. **Severity.** `acceptance_criteria` are free prose. Nothing states what
   observation would show a criterion was *not* met, so a worker can satisfy
   every criterion with a check that could not have failed.
2. **Independence.** Nothing records how a second look differed from the first.
   The contract knows the `agent`, the `bundle_revision` and the `execution`
   tier; it does not know the route. #547 states the principle for one agent's
   self-check; the protocol has no equivalent across workers.
3. **Aggregation.** With more than one result there is no rule for combining
   them, so the parent falls back on agreement — the one statistic that
   correlated error corrupts.
4. **Stopping.** The number of verifiers is a guess, unrelated to the cost of
   accepting a wrong result.
5. **Uncertainty.** Acceptance is binary. A calibration laboratory reports a
   value with an uncertainty and decides by a separately agreed rule; the parent
   reports a result with neither.
6. **Calibration.** Nothing measures how often an accepted result later turned
   out wrong. `src/engine/subagent_ledger.py` records routing facts behind
   `AGENTS_SUBAGENT_LEDGER=1` and summarises acceptance without escalation — a
   proxy for "the structural validator did not complain", not for correctness.
7. **Known-answer controls.** #567 plans a planted-defect benchmark for
   `code_reviewer` offline. Nothing measures the verifier population in the
   configuration that actually runs.
8. **Nowhere for an established fact to live.** Every validation is thrown away
   at the end of the turn. The same claim is re-verified from scratch in the next
   session, at full cost, and a later claim that contradicts an earlier verified
   one is never noticed.

## The findings the design rests on

Each is stated with its source and its limit. The citation list is in the
evidence base at the end, with the provenance of each extraction.

1. **A second agent is not a second measurement, even across models and
   harnesses.** The agentic N-version experiment above is the direct
   measurement. It replicates Knight and Leveson, who built 27 versions of one
   specification at two universities, ran a million tests, and rejected the
   independence hypothesis at 99 % confidence while the individual versions were
   reliable — 23 of 27 correct on more than 99.9 % of cases. The mechanism in
   both cases is shared difficulty: failures concentrate where the specification
   is hard or ambiguous.
2. **Dependence has an exact price, and it rises with fan-out.** Clemen and
   Winkler showed that dependent sources yield in aggregate the same information
   as a smaller number of independent ones, that the incremental value of extra
   sources falls off quickly, and that the attainable precision stays well below
   the independent-source expectation. Ladha's sufficient condition for the
   Condorcet jury theorem is an upper bound on the average pairwise correlation,
   and the bound tightens as the jury grows: a larger fan-out tolerates *less*
   dependence, not more. Positive correlation lowers jury competence, and
   enlarging a positively correlated jury can be detrimental up to a certain
   size before it becomes beneficial again — which is precisely the shape of the
   agentic N-version result.
3. **Below chance, more voters is worse.** If individual competence is under one
   half the jury theorem reverses and the optimal jury is a single voter. So the
   verifier population's accuracy on a task class is not a nice-to-have
   statistic: without it, fan-out may be actively harmful, and the only way to
   get it is a known-answer control set.
4. **Intrinsic self-correction makes results worse; an external signal makes it
   work.** Over two self-correction rounds GPT-4 fell 95.5 → 91.5 → 89.0 on
   GSM8K and 49.0 → 49.0 → 43.0 on HotpotQA, and GPT-3.5 collapsed
   75.8 → 38.1 on CommonSenseQA. With oracle labels telling the model when it
   was wrong, the same loop helped. On a verifiable task a GPT-4 critic marked
   30 of 500 graph colourings correct, of which 5 actually were, while an
   external sound verifier reached nearly 40 % where self-critique left 1 of 100.
5. **Debate does not beat a well-prompted single agent at equal budget.** At
   matched response budgets debate scored 83.2 against self-consistency's 85.3
   at six responses and 83.0 against 88.2 at nine. Across 5 debate methods, 9
   benchmarks and 4 models, debate failed to reliably outperform
   chain-of-thought or self-consistency while consuming more compute; across 7
   benchmarks no debate system beat non-debate prompting overall, at roughly 12×
   the single-agent cost for a 4-point difference. Discussion also propagates
   wrong answers: conformity under a wrong peer majority ranges from 9.2 % to
   95.3 % across models, rises with more discussion rounds, and reverses
   correct answers on 22.8 % of MMLU, 54.8 % of GPQA and 71.0 % of SimpleQA
   items in a large open-weight study.
6. **Blinding is the best-quantified single mechanism in this whole literature.**
   Across 21 trials with 4,391 patients, where the same binary outcome was
   assessed both blind and unblinded, the pooled ratio of odds ratios was 0.64
   (95 % CI 0.43 to 0.96): non-blinded assessors exaggerated effects by 36 % on
   average. Nothing else here has an effect size that clean. The transfer is
   direct: a verifier that sees the generator's conclusion is an unblinded
   assessor.
7. **Same-model judges prefer their own work, measurably.** Judge scores
   correlate with error-overlap similarity at r = 0.84 across 9 judges, 39
   models and 8,707 questions; GPT-4 recognises its own text at 73.5 % and its
   self-preference moves with that recognition; a debate judge favours the side
   running its own model. What nobody has measured is the quantity this protocol
   most needs — the conditional error correlation between two same-model workers
   inside a fan-out-and-validate pipeline.
8. **Deference to the most-credentialed participant is unreliable.** Across 123
   life-science experts in six structured elicitation workshops, peer ratings of
   expertise correlated with accuracy only weakly and sometimes negatively
   (r from −0.39 to 0.22), while self-ratings and peer ratings of expertise
   agreed strongly (r = 0.85). Routing a check to the specialist persona does
   not make its verdict more accurate; it makes it more confident.
9. **There is an information-theoretic floor on what checking can buy.** For any
   test with error probabilities α, β, E₀(T)·KL(f₀‖f₁) ≥ d(α, 1−β). If a
   verifier's verdict distribution barely differs between "the result is right"
   and "the result is wrong", the expected number of verifiers grows like 1/KL
   and no stopping rule evades it. A weak verifier cannot be repaired by buying
   more of them.
10. **Dispersion across independent analyses is itself the measurement.** Given
    one dataset and one question, 29 teams of 61 analysts produced effects from
    0.89 to 2.93 odds-ratio units (median 1.31) using 21 unique covariate
    combinations; 69 % found a significant positive effect and 31 % found none,
    and neither prior beliefs nor expertise explained the variation. When there
    is no ground truth, the spread across routes is the honest output.
11. **A measurement is not a decision, and the separation is the whole point.**
    GUM produces y ± U with a stated coverage factor; a separately agreed,
    documented decision rule then converts that into pass or fail with the risk
    quantified. Deciding on the bare value — "simple acceptance" — is the
    documented case where the false-accept probability reaches 50 % at the
    limit, and UKAS states the consequence plainly: without measurement
    uncertainty in the decision process "the risk remains undefined (or
    uncontrolled) and the conformity decision is worthless". `valid: true` plus
    a reading of the summary is simple acceptance.
12. **Provenance never establishes truth, and every standard in that family says
    so.** An unkeyed digest gives content identity and modification detection
    relative to a digest the verifier already holds, and nothing else; integrity,
    authenticity and non-repudiation are three separate properties; SLSA's level
    table *is* its guarantee and "provenance doesn't do anything unless somebody
    inspects it"; a transparency log does not prevent bad issuance, and a
    misbehaving log can show inconsistent views to different clients; chain of
    custody is procedural, not cryptographic; ALCOA+ is a checkable attribute
    set, not a guarantee that recorded values are right.
13. **An indicator used for control corrupts what it measures.** Campbell: "The
    more any quantitative social indicator is used for social decision-making,
    the more subject it will be to corruption pressures and the more apt it will
    be to distort and corrupt the social processes it is intended to monitor."
    Strathern's restatement is the famous one: "When a measure becomes a target,
    it ceases to be a good measure." Campbell trusted census data precisely
    because it was unused for decisions.
14. **Paying for honesty backfires.** Peer-prediction mechanisms make truthful
    reporting a Nash equilibrium without any verification, but uninformative
    equilibria pay at least as well, and in a real repeated game participants
    settled on them — while in the control condition *without* peer-prediction
    payments most participants stayed consistently truthful. Scoring agents for
    agreement is not a reliability mechanism.

## Design: eight layers

Each layer names what it adds, the finding it rests on, the protocol surface it
touches, the operational test that distinguishes applying it from claiming it,
and the cut rule that removes it if the measurement says it is ceremony.

### Layer 0 — Pre-registration (already paid for, not yet named)

`contract_revision` is already a pre-registration device: task, criteria, scope
and non-goals are fingerprinted before the work exists, and a changed assignment
is a new `attempt_id` with a new fingerprint. The measured effect of
pre-specification is large: 96 % positive results in standard psychology
articles against 44 % in registered reports.

What is missing is the normative sentence that makes use of it: **the parent must
not revise acceptance criteria after reading a result.** Reinterpreting a
criterion so that the returned work satisfies it is the delegation-level form of
hypothesising after the result is known, and the fingerprint already makes it
detectable — the revised criterion cannot be the one the result was produced
against.

The same applies to the acceptance rule. Conformity assessment requires the
decision rule to be agreed *before* the measurements are taken
(ISO/IEC 17025:2017 7.1.3, operationalised by ILAC-G8). How much verification
the parent will buy, and what evidence it will demand, belong in the contract.

- **Surface:** `docs/subagent-contract.md`, the managed template's
  delegated-worker section, and the `acceptance` block of Layer 4.
- **Operational test:** for every accepted result, the criterion text in the
  retained contract is the one the parent judged against.
- **Cut rule:** none needed; it forbids something rather than adding work.

### Layer 1 — Severity: criteria carry their own falsifier

A criterion such as "report actionable findings with evidence or explain the
coverage limits" cannot fail: any output satisfies it. Severe testing requires
the opposite — a test highly capable of finding the flaw had the flaw been there.
Mayo's counterexample is the Texas Marksman, who fires into the side of a barn
and paints the bull's-eye afterwards.

Proposal: `Criterion` gains two optional short fields.

- `falsifier`: the observation that would show this criterion is **not** met.
- `method`: `execution` (a command was run), `source` (a named source was
  opened), `rederivation` (independently recomputed), `inspection` (read and
  judged).

And `Check` gains `criterion_id` and `kind: "own_execution" | "subject"`. The
second value closes #534: a review or research worker that records a defect in
the material it was given sets `kind: "subject"`, and `escalation()` leaves
subject checks out of its `check_failed` count, while a worker whose own command
failed still escalates as today. This plan's own research round hit a second
variant worth fixing in the same change: a repair worker that faithfully
preserved the failed checks of a previous attempt escalated on them, although
its own execution had not failed. `kind` alone does not fix that, because a
copied check inherits the new envelope's `attempt_id` and is indistinguishable
from one the current attempt ran. Each check therefore needs its own provenance:
a `ran_in_attempt` field carrying the `attempt_id` that executed it. Escalation
counts only failed `own_execution` checks whose `ran_in_attempt` equals the
envelope's `attempt_id`, so a preserved check stays visible as evidence without
driving the current attempt up a tier.

That membership test needs a lineage the contract does not currently carry, so
the lineage has to be supplied rather than assumed. The parent knows which
earlier attempts it is asking the worker to preserve checks from — it holds
their receipts — and passes them as a bounded `preserves_attempts` list;
preparation writes them into the contract as `attempt_lineage`, inside the
fingerprint like every other contract field. Validation then accepts a
`ran_in_attempt` only when it equals the envelope's `attempt_id` or appears in
that lineage. When no lineage was supplied the only accepted value is the
current `attempt_id`, and any other is a diagnostic rather than a silently
ignored field — which also keeps a worker from inventing an attempt to park a
failed check in.

Two cheap additions from argumentation theory make the field usable rather than
decorative:

- A falsifier is stated as a **defeater**, and the two kinds are distinguished
  (Pollock): a *rebutting* defeater is a reason to believe the opposite
  conclusion; an *undercutting* defeater is a reason that removes the support
  without asserting the opposite. The difference decides what a refuter has to
  find.
- The criterion's warrant is named when it is not obvious — Toulmin's layout
  (claim, data, warrant, backing, qualifier, rebuttal) is the minimal vocabulary,
  and the `qualifier` and `rebuttal` slots are exactly what the envelope lacks.

- **Surface:** `src/schemas/subagent.py` (`Criterion`, `Check`),
  `src/engine/subagent_validation.py`, `src/engine/subagent_text.py`,
  `src/engine/model_tiers.py` (`escalation`),
  `rules/subagents/rule-worker-result.mdc`.
- **Operational test:** a criterion whose `falsifier` names an observation the
  worker's `scope` could not have produced is a weak test, and the parent can
  detect that mechanically. The validator can require a nonempty `falsifier`
  whenever `method` is `execution` or `rederivation`.
- **Honest warning from the same literature:** structured argument frameworks
  institutionalise confirmation bias when the argument is built to support a
  conclusion already chosen — Leveson's critique of safety cases — and a fallacy
  taxonomy applied to real industrial safety arguments found fallacies in every
  case sampled. A `falsifier` field inherits that risk; the cut rule below is
  the defence.
- **Cut rule:** if planted-defect recall does not rise beyond the noise band
  when criteria carry falsifiers, drop both fields and keep `Check.kind`, which
  pays for itself by removing the #534 escalations.

### Layer 2 — Independence: the route is the unit of evidence, not the agent

This is the centre of the plan. A verification attempt must record how it differs
from the attempt it checks, because a check that reaches the answer by the same
route as the claim is not a check — #547 states this for one agent, and findings
1, 2, 6 and 7 extend it across workers. The classical name for the requirement
is convergent validation across independent methods (Campbell and Fiske's
multitrait-multimethod matrix); the classical name for its absence is
common-mode failure.

Proposal: a bounded `route` descriptor, recorded per attempt and fingerprinted
with the contract.

```
Route {
  model:           the resolved model, or null for tier `inherit`
  lineage:         the model family, where the server can resolve it
  decomposition:   a short parent-assigned id for the framing or split used
  information_set: the pinned manifest id, or an id for the named materials
  stance:          produce | refute | rederive | replicate
  blinded:         whether the worker was shown the checked conclusion
}
```

Preparation gains two arguments: `verifies: {task_id, attempt_id,
contract_revision}` and `independence: [dimension, ...]`, the dimensions the
parent asserts differ from the verified attempt.

Then the definition the protocol needs: **a verification that differs on no
dimension is a replication of the same measurement**, detecting sampling noise
and nothing else. With `independence` empty the receipt carries a warning and
the parent must record the result as same-route.

What the server can and cannot enforce has to be stated exactly, because the
obvious enforcement does not work. A `model` independence claim can be checked
only when both attempts carry a resolved model identity. Tier `inherit` leaves
`execution.model` null — the host picks the model and the contract never learns
it — so for an `inherit` attempt on either side the claim is neither confirmed
nor refuted: preparation marks it `unverifiable`, and an unverifiable claim is
recorded as route-different-unverified, never as model independence. An unknown
model is not evidence of difference any more than it is evidence of equality.
Making the claim checkable requires the host to report the model it actually
launched and preparation to bind that identity to the attempt.

Resolving a tier is not the same as knowing the model, so even the lower tiers
do not settle it by themselves. Tier resolution can yield an alias or a
configured override rather than a model identity — on one client the aliases are
deliberately stable names that always point at the newest model of a class — so
two different recorded names can select the same model, and one name can select
different models a month apart. Comparing names therefore establishes a
difference only where the two names are known to denote distinct models;
otherwise the claim is `unverifiable` for the same reason as at `inherit`. What
makes it decidable is a bound actual identity, and nothing less.

The same honesty applies to the contract the claim points at. `verifies` can be
resolved server-side only while the stored contract of that `contract_revision`
is still on disk; with `delivery="inline"`, or after the seven-day cleanup, only
the parent holds it. In that case preparation accepts the retained contract
object for the verified attempt, and when neither is available it refuses the
`independence` claim rather than assuming the routes differ.

Three mechanisms fill the dimensions, in descending order of evidence.

- **Blind the verifier** (finding 6, the 36 % exaggeration by unblinded
  assessors). `blinded: true` means the worker received the task and the
  materials but not the conclusion under check. This is cheap, needs no second
  model, and has the best effect size in the literature. #563 is the nearest
  existing shape but it is deliberately *not* blinded: it hands the worker the
  decline reason, which is the conclusion under check, and withholds only the
  session's wider reasoning. It therefore records as route-different with
  `blinded: false`, and a fully blinded variant — the finding and the code
  without the decline — is a separate arm worth measuring against it.
- **Change the model family** (finding 1 and the heterogeneity result: a mixed
  model pool scored 76.7 on a nine-benchmark average against 72.1 for the
  homogeneous mean). This is the analogue of diverse double-compiling, the
  accepted countermeasure to Thompson's trusting-trust attack — a compiler
  cannot verify itself, and the fix is an independently produced second
  compiler, not a more careful reading of the first. The repository already has
  the opt-in local-inference broker (`src/local_inference/`), which is the one
  genuinely different-lineage model an installation may have available. Note the
  honest ceiling: the agentic N-version experiment used 23 models across 5
  harnesses and still measured strong common-mode failure, so a different model
  reduces dependence, it does not remove it.
- **Force a different derivation from the same worker** when no second model is
  available. Dialectical bootstrapping — take a second estimate generated under
  deliberately different assumptions and average it with the first — has a
  measured benefit beyond a pure reliability gain and a high-powered
  pre-registered replication. This is the weakest of the three and should be
  recorded as such.

`stance: refute` makes the asymmetry explicit: a refuter's success condition is
finding a defeater, which is both #563's shape and the only form in which finding
4's "external signal" is available without ground truth.

- **Surface:** `src/schemas/subagent.py` (`TaskContract`, `PreparationRequest`),
  `src/engine/subagent.py` (`_assemble`), `src/engine/model_tiers.py`,
  `docs/subagent-contract.md`, and one sentence in the managed template.
- **Operational test:** for every accepted result that was checked, the parent
  can name the dimension on which the check differed. "A second reviewer agreed"
  with no named dimension is not a check.
- **Honest limit:** `model` and `blinded` have measured payoffs.
  `decomposition` and `information_set` are reasoned by analogy with forced
  diversity and with #547's field observations; no retrieved study measures them.
- **Cut rule:** if route-different verification finds no more planted defects
  than same-route verification on the #567 set, keep `blinded`, `model` and
  `stance` and drop the rest.

### Layer 3 — Aggregation under declared dependence

Do not count verdicts; count effective verdicts, and never assume independence
you have not measured.

The arithmetic is standard. With n verdicts sharing a route dimension at
correlation ρ, the design effect is `DE = 1 + (n − 1)ρ` and the effective count
is `n / DE`; in the source's own case study 128 clustered observations behaved
like about 84, and a design powered at 80 % delivered 61 %. Three verdicts from
identically prepared same-model workers at ρ = 0.8 give DE = 1 + 2 × 0.8 = 2.6
and are therefore worth 3 / 2.6 ≈ 1.15 independent verdicts. Clemen and Winkler's result says the same thing from the
other direction: dependent sources are equivalent to a smaller number of
independent ones, and the incremental value of extra sources falls off quickly.

Two dependence statistics, one structural and one statistical, both computable
without ground truth:

- **Evidence overlap**, available today with no new machinery. Two results that
  cite the same `evidence[].reference` strings for the same claim are not
  independent on the information set. The envelope already carries those
  references.
- **The rank-one test.** If classifiers' errors are conditionally independent,
  the off-diagonal entries of their verdict covariance matrix form a rank-one
  matrix whose leading eigenvector entries scale with each classifier's balanced
  accuracy (Parisi et al.). Departure from rank-one is evidence of dependence,
  and the same eigenvector gives an unsupervised accuracy ranking — but only up
  to the eigenvector's sign, which no amount of unlabelled data resolves. The
  source's own tie-breaker is the assumption that most of the classifiers are
  better than random; where that cannot be asserted for a task class, the
  control set has to supply the orientation, and until one of the two holds the
  ranking is reported as ambiguous rather than used as a weight. Jaffe et al.
  supply the matching negative result and remedy: ensembles built on conditional
  independence "can be severely sub-optimal" when it is violated, and dependent
  members can be detected without labels.

What the plan deliberately does **not** adopt:

- **Plain majority vote** as the decision rule. The Condorcet result needs
  independence and competence above one half; finding 3 says that below one half
  the optimal jury is a single voter, and findings 1 and 2 say the independence
  premise is false here. Majority voting over diverse implementations of a
  *sharply specified* unit does measurably help (the 387.44 → 130.99 result), so
  the rule is not "never vote" but "vote only where the unit is small, the
  specification is unambiguous, and the coincident-failure rate has been
  measured". The same conclusion arrives from the other end: keeping each unit
  small is what makes voting affordable, because per-step error rose from about
  1 in 1,000 below roughly 750 response tokens to about 1 in 10 above it.
- **Weighted majority with log-odds weights** as a default. Nitzan and Paroush
  proved that the optimal rule in the uncertain dichotomous choice model is a
  weighted majority with weights ln(p/(1−p)), but the model assumes independent
  experts; with correlated agents those weights double-count shared evidence.
  Weights are usable only after Layer 6 supplies per-route accuracy *and* a
  dependence discount.
- **Peer prediction, Bayesian truth serum or any scoring-for-honesty scheme**
  (finding 14). Uninformative equilibria pay as well as truthful ones, and the
  experiment found participants settling on them while the unpaid control group
  stayed truthful. Prelec's surprisingly-popular rule reports real gains — 21.3 %
  error reduction against majority vote and 24.2 % against confidence-weighted
  vote — and belongs in the evaluation backlog, but not in the protocol.
- **Per-claim voting across a result set.** List and Pettit's impossibility
  result says that on an agenda closed under negation containing p, q and p∧q,
  no rule satisfies universal domain, collective rationality, anonymity and
  systematicity together. Aggregating claim by claim and expecting a consistent
  whole is not available; aggregate at the level of the decision instead.
- **The diversity-prediction story as an argument.** Page's identity
  γ² = ε − δ needs no independence assumption because it is an identity, but the
  common reading that raising diversity lowers collective error is not correct,
  since the two terms cannot be varied independently; and the Hong-Page
  "mathematical theorem" has been shown to be close to a restatement of its own
  hypotheses. Use the identity to decompose an observed error, not to justify
  adding workers.

What the plan does adopt is asymmetry and arithmetic. **One sound refutation with
a named defeater outranks k agreements**, because agreement is cheap and
correlated while a defeater is concrete and checkable by a third route — the
Neyman-Pearson shape, where the error you care about is fixed first. And where
several workers do give probability judgments, enforce internal coherence before
aggregating: in the intelligence-analyst cohort, making each analyst's
judgments respect the unitarity axiom and *then* aggregating cut mean absolute
error by 61 %, which is a larger effect than any thinking protocol measured in
the same literature.

- **Surface:** parent-side procedure in `docs/subagent-contract.md` and the flows
  that fan out; no schema change beyond Layer 2's `route`.
- **Cut rule:** if the evidence-overlap statistic never changes a disposition
  across an evaluation run, drop it and keep only the route record.

### Layer 4 — Stopping: cost-driven and valid under looking

The number of verifiers should follow from the cost of being wrong. Four rules,
in increasing order of machinery.

**Ask first whether any verdict could change the decision.** If the currently
optimal action stays optimal for almost every outcome the proposed check could
return, the expected value of that sample information is zero. This needs no
statistics: before spawning a verifier, name the verdict that would change the
decision. If there is none, stop.

**Keep the two cost decisions apart.** They have different thresholds, and
conflating them is easy. The first is *accept or reject what you have*, a
decision under misclassification costs: with `C_accept_bad` the cost of
accepting a wrong result and `C_reject_good` the cost of rejecting a good one,
the Bayes rule accepts when the posterior probability that the result is
correct is at least `C_accept_bad / (C_accept_bad + C_reject_good)`. Because
accepting a wrong result is normally far more costly, that threshold sits close
to 1 — a result has to be strongly supported to be accepted — and the threshold
is a ratio of the two *decision* costs, not of anything to do with the price of
a check.

The second decision is *buy another check*, and it compares the price of the
check against the loss the check could actually remove. That is the current
minimum loss over the actions already available, not the loss of accepting. With
p the probability that the result is wrong,

```
L0 = min( p · C_accept_bad ,  (1 - p) · C_reject_good )
```

and a perfect check is worth buying only while `L0 > c_check`. The reason the
distinction bites is that rejecting is often cheap: with p = 0.9,
`C_accept_bad` = 100 and `C_reject_good` = 1, accepting costs 90 but rejecting
costs 0.1, so a perfect check priced at 1 cannot pay for itself even though the
result is almost certainly wrong. Verification is worth buying only when *both*
actions are expensive — when the result is genuinely needed and cannot simply be
discarded. Comparing `c_check` against `p · C_accept_bad` instead of against L0
buys checks on work that should just be thrown away.

With a real verifier the comparison has to run over the verdicts rather than
over a single number, because a verdict can also be wrong in the direction that
costs money. For a check with detection rate d on a defective result and
false-flag rate f on a sound one, the quantity to compare with `c_check` is
`L0 − E_v[ min over actions of the expected loss given verdict v ]`, where the
expectation runs over the verdicts the check can return. Missed defects leave
part of `p · C_accept_bad` in place; false flags add rejections of sound
results. Layer 6 is where d and f come from; while either is unknown this rule
cannot be applied at all, and the honest fallback is the zero-value question
above plus a declared `max_attempts`.

**Do not fix N; use a rule that stays valid while you look.** Each verification
can contribute a conditional e-value; the running product is a test
supermartingale, and P(∃m : E⁽ᵐ⁾ ≥ 1/α) ≤ α holds uniformly over every stopping
time and every data-dependent decision to continue. That is exactly what an
adaptive parent does — read a verdict, decide whether to buy another — and it is
the one framework under which the practice carries a stated error bound. A
fixed-sample threshold read under adaptive stopping carries none.

The guarantee is conditional on a requirement that this setting makes hard, and
it must be stated rather than assumed: each stage's e-value has to satisfy
`E[E_m | F_{m-1}] ≤ 1` under the null *given the routes and verdicts already
seen*. Per-route sensitivity and specificity averaged over a task class are not
enough; what is needed is the verdict distribution of the next route conditional
on the previous ones, which is exactly what common-mode failure corrupts. Two
same-lineage verifiers that fail together on hard inputs break the conditional
bound, and the product then carries no error control at all. So the e-process is
usable only where Layer 6 has measured that conditional behaviour on the control
set; until then the plan claims no formal guarantee for it.

**Know when verification stops paying at all.** Deming's all-or-none rule: with p
the error rate of incoming work, k₁ the cost of one check and k₂ the cost of
letting an error through, minimum average cost comes from **no** checking when
p < k₁/k₂ and from checking **everything** when p > k₁/k₂; an intermediate
sampling plan is optimal only at the break-even point. "Spot-check a fraction of
worker outputs" is almost never the optimal policy.

That rule assumes the check catches the defect it looks at, which an inspector
in a factory approximately does and an LLM verifier demonstrably does not. With
detection rate d the break-even moves to `p · d > k₁/k₂`, so a weak verifier
pushes the all-or-none boundary towards "do not check" — not towards "check
more", which is the intuitive and wrong response. Applying the undiscounted rule
with a detection rate of, say, 0.3 would prescribe checking everything on
workloads where checking loses money, so d belongs in the comparison from the
start.

Four assumptions come with the rule even in that corrected form, and a workload
that breaks any of them needs the full verdict-wise comparison above instead.
Detecting a defect is taken to avoid the whole of k₂ at no further correction
cost; a false flag on a sound result is taken to cost nothing, or to be already
inside k₁; the expected cost and benefit of a check are taken not to vary with
the fraction of work inspected, which is what makes the optimum all-or-none
rather than partial; and p is taken to be stable over the batch, which is the
statistical-control precondition the original rule states in its own terms. The
third is the one most likely to fail here, because a parent that checks only the
hard cases is choosing a non-uniform sampling fraction on purpose.

Proposal: an optional, pre-registered `acceptance` block in the contract.

```
Acceptance {
  accept_costs:         C_accept_bad and C_reject_good, giving the acceptance
                        threshold, or alpha directly
  check_cost:           c_check, for the sampling comparison
  stopping:             single | sequential
  max_attempts:         the budget ceiling the parent will not exceed
  dependence_threshold: the measured route dependence above which a check
                        yields accepted_conditional instead of accepted
}
```

`escalation` then follows from this block rather than from the tier table alone,
which gives #534 and #443 a principled basis: a result climbs a tier because the
evidence has not crossed the pre-registered threshold, not because a reviewer
recorded a finding.

- **Surface:** `src/schemas/subagent.py` (`TaskContract`),
  `src/engine/model_tiers.py`, `docs/subagent-model-tiers.md`.
- **Hard precondition:** the e-value needs per-route sensitivity and
  specificity. Without Layer 6 there is no number to put in it, and finding 9
  says a weak verifier cannot be fixed by buying more. Until the ledger has
  data, the honest implementation is the zero-value check, the cost ratio and a
  declared `max_attempts`.
- **Cut rule:** if the ledger shows verifier KL divergence too small for the
  sequential rule to terminate inside `max_attempts` on real tasks, keep the
  zero-value check and the all-or-none policy and drop the e-process.

### Layer 5 — The accepted result carries an uncertainty, and the decision is separate

Finding 11 is a procedure, not an attitude. Metrology separates three things the
protocol currently fuses: the value, its uncertainty, and the decision taken on
it. The vocabulary discipline is part of it — trueness, precision and accuracy
are distinct, and accuracy is not a number, so "this result is accurate" is not a
reportable claim while "a blinded different-model refuter found no defeater" is.

Proposal, all parent-side and no schema change:

- Four dispositions with fixed meanings, because a route that merely *differs*
  is not an independent one and because the stopping rule can legitimately stop
  before any check runs. `accepted` — criteria met, checked by a route-different
  method, and the measured dependence for that pair of routes is below the
  threshold the contract pre-registered. `accepted_conditional` — criteria met
  and checked, but the check was same-route, unblinded, claimed an independence
  the server could not verify, or the stated falsifier was not observable; the
  limitation is named. `accepted_unchecked` — criteria met and no check was
  bought, because the claim's importance was `routine` or because the
  value-of-information rule said no verdict could change the decision; the
  reason is named. `rejected`.
- Only `accepted` may be reported as verified. Until Layer 6 has measured a
  dependence threshold for a task class, a route-different check yields
  `accepted_conditional`, not `accepted` — the plan's own risks section says
  heterogeneity is not independence, and the disposition has to say the same.
- `accepted_conditional` and `accepted_unchecked` both reach the user. Deciding
  on the bare value is simple acceptance, whose false-accept risk the standards
  put at up to 50 % at the limit.
- The qualifier is a **separate delegation marker**, not a reuse of the existing
  scale. `skill-confidence-markers` fixes HIGH as "checked this turn", and an
  ordinary verified edit legitimately earns it; redefining HIGH as "checked by a
  route-different method" would change the meaning of a shared component for
  every other consumer and mislabel ordinary work. The delegation marker is the
  disposition itself, which is auditable because the route record backs it, and
  the two vocabularies stay independent. Extending the skill instead is possible
  but is its own change, with its own consumers to update.
- Where no ground truth exists and several routes were run, report the
  dispersion (finding 10) rather than a verdict, and call it what the standards
  call it: a degree of equivalence, not accuracy.

- **Surface:** `docs/subagent-contract.md` (#326's peer-rights section), the
  Delegation paragraph #326 specifies, and the integrating flows.
- **Operational test:** count the `accepted_conditional` dispositions that
  reached the user. A parent that never issues one, on a workload where most
  checks are same-route, is not applying the layer.
- **Cut rule:** if blind judging finds that conditional dispositions change no
  user decision, collapse them into `accepted` with a named limitation.

### Layer 6 — The system measures itself

Metrology's answer to "how do you know the measurer works" has three forms, with
three different strengths, and the standards name the weakest one honestly.

| Metrology form | What it needs | Transfer |
|---|---|---|
| Proficiency testing: the provider holds the assigned value and scores each participant by z = (xᵢ − x_pt)/σ_pt, with \|z\| ≤ 2 satisfactory, 2–3 warning, ≥ 3 action | A blind, homogeneous test item and a justified assigned value | #567's planted-defect benchmark, generalised beyond `code_reviewer` to every role used as a verifier, and re-run rather than run once |
| Certified reference materials: the known value travels with the item and its certificate, carrying its own traceable uncertainty | Homogeneity, stability, and a value not also used as the calibrator | A small frozen set of tasks with known answers, kept out of every prompt, rule and eval example, used as the trueness control in the configuration that actually runs |
| Key comparisons: no external truth exists, the reference value comes from the participants, and the output is a *degree of equivalence* — a deviation plus the expanded uncertainty of that deviation | A protocol fixing the reference-value method before results are seen | k route-different workers on one task, reporting dispersion rather than a verdict |

**The ledger.** Extend `src/engine/subagent_ledger.py` from routing facts to
outcomes: per (agent, complexity, route dimensions), how often an accepted
result was later refuted — by a failing test, by CI, by a human, by a later
session.

A refutation is only half an operating characteristic, and the ledger has to be
designed for the other half. An accepted result that later turns out wrong is a
false-accept observation; it supplies no denominator for the opposite error,
because results the verifier rejected do not come back labelled. Sensitivity and
specificity therefore come from the control set, where the answer is known: the
ledger records, per attempt, the route, the verifier's verdict, the known answer
where there is one, and which outcome class the pair falls into, with the
positive class defined once and written down ("the verifier says the result is
defective"). The control set supplies the full confusion matrix, and it is the
only source of d and f that Layer 4 can use.

Production refutations are a weaker quantity and must be labelled as one. They
count only the wrong results somebody happened to notice, so the rate they give
is an observed-refutation rate, a lower bound on the field false-accept rate
rather than an estimate of it. Turning it into an estimate would need either
follow-up on a known fraction of accepted results, or an assumption about what
share of errors eventually surface — and both are assumptions, not measurements.
Until one of them is in place the ledger reports the observed-refutation rate
under that name and does not call it the field error rate. The ρ estimates
Layer 3 needs come from coincident-failure counts across route pairs on the same
control items, which is again the control set rather than production. Keep the current privacy shape exactly: opt-in
behind the environment variable, hashed task IDs, routing and outcome facts only,
never task text, and a write failure that never fails a tool call.

Three measurement traps to avoid in that ledger, all documented:

- **Agreement-based accuracy is inflated.** Dawid-Skene style estimation assumes
  conditional independence; shared error is absorbed into the confusion matrices,
  which then overstate individual accuracy. Estimate accuracy against the control
  set, not against peer agreement.
- **Binned calibration error understates the truth.** Binning satisfies
  CE(f_B) ≤ CE(f) for any binning scheme, with a published example where the
  binned estimate is 0 while the true calibration error is at least 0.49, and the
  usual plug-in ECE is biased. Use a debiased estimator and equal-mass bins, or
  report no calibration number.
- **Kappa misleads under imbalanced marginals.** Kappa can be low despite high
  observed agreement, and higher for asymmetrically than symmetrically imbalanced
  marginals. Since most results are accepted, the marginals here are imbalanced
  by construction; report separate positive and negative agreement indexes.

**Drift.** Once the ledger has a baseline, Shewhart limits at k = 3 with the
Western Electric run rules separate a real change in verifier quality from
ordinary variation, and GUM names control charts as the evidence that a
measurement process stays in control. This matters because the measurer changes
under us: pin model snapshots and prompt revisions with their scores, and re-run
the control set on every change.

**The reflexive hazard** (finding 13), with three binding consequences:

- **Keep one unused channel.** The control set must never be fed back into
  prompts, rules, skills or eval examples; the moment it is, it stops measuring.
- **Ceremony detectors with a cut rule**, exactly as #327 specifies: a mechanism
  that fires on more than two thirds of runs without a recall gain gets narrowed
  or cut. Every layer above carries one.
- **State the predicted effect before the run.** Without an accepted external
  criterion, the only available validation is an explicit, publicly checkable
  network of predictions — which is also why a construct is, in Cronbach and
  Meehl's words, "at best adopted, never demonstrated to be 'correct'".

The cautionary case is already in this project's own citations: Cronbach's alpha
is a well-defined lower bound routinely read as a claim it cannot support.
`verification: structural_only` is this project's alpha.

### Layer 7 — Importance, and a protected store for validated facts

The two requirements are one mechanism: rate a claim's importance, validate the
important ones, and keep what was established so it is not re-established from
scratch next session.

**Is an importance rating needed?** Yes — findings 9 and the all-or-none rule
make it unavoidable, because the decision "verify or not" is a comparison of the
claim's consequence against the cost of a check, and without a rating there is
no left-hand side. But it must be *derived from structure, not asserted*, for
exactly the reason in finding 13: a self-assigned score that unlocks less work
will drift downward.

Three structural questions, answerable without judgement about content:

1. **Load-bearing?** Does any decision, edit or further claim depend on it? If
   nothing does, its importance is `none` — the value-of-information rule says a
   check that cannot change a decision has zero value. Do not verify, do not
   store.
2. **Reversible?** Can a wrong value be detected and undone cheaply later?
   Irreversible raises the rating.
3. **Durable?** Will it be reused beyond this turn? Durable raises the rating,
   and makes it a candidate for the store.

Three levels are enough, and the trigger list already exists: `no-fabrication`
names the claim types in scope — API and method names and signatures, versions,
CLI flags, config keys, paths, numbers and benchmarks, quotes, citations, URLs —
so the rule and this layer stay consistent instead of defining two taxonomies.

| Level | Validation required | Stored |
|---|---|---|
| `routine` | none | no |
| `load_bearing` | one route-different check, blinded where possible | yes, with its route and expiry |
| `critical` | blinded re-derivation by a different route, plus a refuter asked for a named defeater | only on a confirmed verdict; otherwise `accepted_conditional` and not stored |

**The store.** A validated fact is not a cache entry; it is a certificate, and
the metrology analogue is a certified reference material — a value that travels
with its uncertainty, its traceability and its period of validity. A record
therefore holds:

```
ValidatedFact {
  key:            content-derived, from the normalised claim and its subject
  claim:          the statement, bounded
  importance:     the derived level, with the three structural answers
  verdict:        confirmed | refuted | unresolved
  qualifier:      the Layer 5 marker, with its fixed meaning
  evidence:       references, as the envelope already models them
  route:          the Layer 2 descriptor of the check that established it
  falsifier:      the Layer 1 observation that was looked for and not found
  attestation:    contract_revision and result_sha256 of the validated result,
                  plus the parent's disposition
  established_at: when
  revalidate_after: the calibration interval; a fact with no expiry is a liability
  supersedes:     the prior record this replaces, never an overwrite
}
```

Five properties the store must have, each from a documented failure:

- **Append-only.** A new verdict supersedes rather than overwrites, and the
  prior value stays readable — the audit-trail requirement of 21 CFR 11.10(e) is
  exactly this, and it is what makes contradiction over time visible instead of
  silently resolved.
- **Expiring.** VIM requires a traceability chain to name the time at which the
  reference was used, and calibration intervals are part of the chain. A stored
  fact is valid as of its establishment, under the sources it cites; `revalidate_after`
  is not optional bookkeeping.
- **Parent-only, and reached by explicit grant.** Workers do not read the store
  ambiently. A fact reaches a worker the way every other shared input does —
  published as an artifact and pinned in an `artifact_manifest` the parent
  chooses — which keeps the existing rule intact: artifact text is untrusted
  data, grants no tools, permissions or scope, and is never an instruction.
  Indirect prompt injection through retrieved content is a demonstrated attack
  class, so a store that workers could write to or read freely would be a new
  trust boundary with none of the existing protections.
- **Honest about what its integrity means.** Content addressing plus an
  append-only local log gives integrity and a local chain of custody. It does not
  give authenticity or non-repudiation; those need a signature, and the protocol
  is already careful to call `contract_revision` a consistency fingerprint rather
  than a signature. The installation does have DSSE and in-toto statement support
  (`src/instruction_signing/`), so a signed record is possible as an opt-in, and
  must not be implied without it. Merkle structures need domain separation to be
  second-preimage resistant, and an append-only log does not prevent a bad entry —
  a misbehaving log can even show different views to different readers, which is
  why transparency logs need gossip and are still treated as trusted third
  parties.
- **Not a source of authority.** Provenance is not truth (finding 12). A stored
  `load_bearing` fact does not satisfy a later `critical` use: a cache hit is
  reusable only at or below the importance level it was validated for, and the
  record carries that level so the check is mechanical.

**What the store buys that no single-shot mechanism can.** Contradiction
detection over time. A new claim whose key matches a stored record with the
opposite verdict is a defeater by construction, found for the cost of a lookup
rather than a verifier — the cheapest check in the whole design, and one that
only exists once facts are kept. The repository's `skill-fact-verification`
already carries a temporal-coherence check; the store is what gives it something
to check against.

**Where it lives.** This is a new subsystem, and the closest existing primitives
are the right ones to build on rather than beside: `src/artifacts/` for
immutable, hashed, manifest-pinned, expiring text; `src/memory/` for the
append-only per-project history that `log_interaction` already writes; the
installation's private data directory for 0700/0600 local storage, as
`data/subagent-exchange/` already uses; and `src/user_sync/` for opt-in
cross-machine sync of personal data through the user's own private repository,
which is where a per-user store belongs if it syncs at all. One point of
confusion worth recording: `docs/shared-kv-mcp/` is the shared **prompt KV cache**
for local inference, with optional Redis snapshot storage — not a fact store, and
not a candidate.

- **Operational test:** the share of `load_bearing` and `critical` claims in a
  turn that were either validated or explicitly marked unvalidated, and the
  share of store reads that hit a record still inside its `revalidate_after`.
- **Cut rule:** if the hit rate on a real workload is negligible, or if stored
  facts are more often stale than fresh at read time, the store is a liability
  and the layer reduces to the importance rating alone.

## Mapping to the protocol surface

| Change | Layer | File | Kind |
|---|---|---|---|
| Criteria are not revised after reading a result; the decision rule is agreed before measuring | 0 | `docs/subagent-contract.md`, `src/engine/protocol/` | instruction |
| "A check that reaches the answer by the claim's own route is not a check" | 2 | `src/engine/protocol/`, `rules/subagents/rule-worker-result.mdc` | instruction |
| Four parent dispositions, with the conditional and unchecked ones reaching the user | 5 | `docs/subagent-contract.md`, `flows/pr-review.md`, `flows/code-review-pass.md` | instruction |
| `Check.kind: own_execution \| subject`, `Check.criterion_id` and `Check.ran_in_attempt`; `check_failed` counts only own-execution checks from the current attempt | 1 | `src/schemas/subagent.py`, `src/engine/model_tiers.py`, `src/engine/subagent_text.py` | schema |
| `Criterion.falsifier`, `Criterion.method` | 1 | `src/schemas/subagent.py`, `src/engine/subagent_validation.py` | schema |
| `Route` with `blinded`, plus `verifies` and `independence`; same-model refusal at preparation | 2 | `src/schemas/subagent.py`, `src/engine/subagent.py`, `src/engine/model_tiers.py` | schema |
| Evidence overlap and the rank-one dependence test; effective-verdict count | 3 | parent procedure, documented | procedure |
| `Acceptance {accept_costs, check_cost, stopping, max_attempts, dependence_threshold}`; escalation driven by it | 4 | `src/schemas/subagent.py`, `src/engine/model_tiers.py`, `docs/subagent-model-tiers.md` | schema |
| A host-reported resolved model bound to the attempt, so a `model` independence claim is checkable at tier `inherit` | 2 | `src/engine/model_tiers.py`, `docs/subagent-model-tiers.md` | schema |
| Ledger outcomes, per-route sensitivity, ρ estimates, control chart, debiased calibration | 6 | `src/engine/subagent_ledger.py`, `scripts/subagent_tiers_report.py` | subsystem |
| Known-answer control set, re-run on every model or prompt change | 6 | `evals/` (generalising #567) | evaluation |
| Derived `importance`; `ValidatedFact` store, append-only, expiring, parent-only, granted to workers only as pinned artifacts | 7 | new module beside `src/artifacts/` and `src/memory/`; `src/user_sync/scope.py` for opt-in sync | subsystem |

## Self-application: how we would know this works

The repository already has the statistical machinery, which is why this can be
specified concretely: `evals/scripts/bench_significance.py` has exact two-sided
binomial p-values, Wilson score intervals, paired McNemar comparison and the
Wilcoxon signed-rank test; `evals/judges/pairwise_judge.py` already judges in
both positions and aggregates with a swap so an additive left/right position
bias cancels, degrading a disagreement to a tie. #327's design — arms, several
seeds, pinned routing, decision rule stated before the run — is the template.

- **Arms.** Current packages against packages carrying falsifiers and route
  records; and, separately, blinded against unblinded verification, and
  same-model against different-model verification, on the identical finding set.
  The blinding arm is the one with a prior effect size worth powering for.
- **Sets.** (A) planted defects with the expected location; (B) clean tasks,
  where any medium-or-above finding is a false positive unless a blind judge
  sustains it; (C) adversarial materials that instruct a worker to confirm;
  (D) a frozen control set that enters no prompt or example; (E) mis-rated
  importance as its own defect class, so the rating's accuracy is measured and
  not assumed.
- **Metrics.** Planted-defect recall with the correct location; false-positive
  rate on clean tasks, judged blind by a model different from the one under test;
  the share of accepted results later refuted, from the ledger; cost per accepted
  result including retries; dispersion across route-different workers on set D;
  and the observed coincident-failure count against the independence prediction,
  which is the one statistic that directly measures ρ.
- **Sample size, stated before the run.** The repository's recorded noise band
  already implies that about 50 cases detect only large differences. Report
  paired per-item differences with intervals, and treat verdicts sharing a model,
  prompt or seed as clustered — the design effect applies to the evaluation
  exactly as it applies to the verification it measures.
- **Judge hygiene.** Pin the judge model snapshot and prompt and store them with
  every score; judge with a different model than the generator, because models
  favour their own output; keep the positional swap.
- **Cut rules, written down first.** Each layer's cut rule is part of the
  pre-registration, not a reaction to the result.

## Risks and honest limits

- **Context cost.** #325 measured the /2 default budget at 20,000 characters with
  the envelope schema at +1,000 to +2,500 and the worker-voice rule at 900 to
  1,300, against a recorded /1 range of 11,787 to 13,290. `Criterion.falsifier`
  and `Route` add text to every package. Measure with
  `evals/scripts/measure_subagent_context.py` for every shipped profile and all
  result types before adopting, and treat a mandatory-context overflow as the
  hard `context_budget_exceeded` it already is.
- **Ceremony.** A `falsifier` invites a formulaic sentence; a `route` invites a
  dimension that differs in name only; an `importance` rating invites inflation
  in both directions. Each has a detector and a cut rule above, and the
  assurance-case literature is the warning: structured argument frameworks were
  measured to contain fallacies in every real case sampled, and their best-known
  critique is that they institutionalise confirmation bias.
- **Unmeasured dimensions.** Only `blinded` and `model` have measured payoffs.
  `decomposition` and `information_set` are reasoned, and may not pay.
- **Heterogeneity is not independence.** Even 23 models across 5 harnesses
  retained strong common-mode failure. The plan's own central mechanism reduces
  dependence; it does not deliver the independence the classical aggregation
  theorems assume, which is why Layer 3 measures ρ instead of assuming it away.
- **No ground truth for most tasks.** The control set covers a thin slice. For
  everything else the honest output is a degree of equivalence, and this plan
  should be read as making that statement possible rather than as promising
  correctness.
- **Goodhart on the ledger and on the store.** Once an acceptance rate is tracked
  there is pressure to accept; once facts are cached there is pressure to reuse
  them past their validity. The mitigations are structural: the control set is
  used for nothing else, the ledger records dispositions rather than a score
  anyone is rewarded on, and a cache hit is reusable only at or below its
  validated importance level.
- **Provenance is not truth, and this plan adds no cryptographic claim.** See
  finding 12 and Layer 7's integrity paragraph.
- **Untrusted evidence.** Worker evidence may quote external content, and
  indirect prompt injection through retrieved content is a demonstrated attack
  class. Nothing here relaxes the existing rule that artifact and task material
  are data, and the route record must never be taken from worker text.

## Order of work

Cheap first, each step independently useful, everything after step 2 gated on a
measurement in #327's form.

1. Instruction only: Layer 0's two sentences, Layer 2's same-route sentence,
   Layer 5's four dispositions. No schema change.
2. `Check.kind` and `Check.criterion_id`, with `escalation()` ignoring subject
   checks and `ran_in_attempt` binding each check to the attempt that ran it.
   Closes #534 and stops the
   spurious escalations #443 would otherwise measure.
3. `blinded` verification as the default shape for every check: the verifier
   receives the artefact and the materials, and not the conclusion under check.
   That goes further than #563's materials rule, which withholds the session's
   reasoning but still hands over the decline reason — so a blinded verifier
   must not receive the decline either, and #563's existing shape records as
   route-different with `blinded: false`.
4. `Criterion.falsifier` and `Criterion.method`, with the validator's conditional
   requirement.
5. `Route`, `verifies`, `independence`, and the server-side same-model refusal.
6. Ledger outcome recording: the route, the verdict, the known answer where
   there is one, and the outcome class. Recording only; no derived statistic
   yet.
7. The known-answer control set, generalising #567, with periodic
   re-measurement and a control chart. This has to precede any measurement of
   sensitivity, because the control set is where the labels come from.
8. Per-route sensitivity, specificity and ρ, measured on that control set; then
   the `Acceptance` block and the sequential rule, which cannot be implemented
   honestly before those numbers exist.
9. Derived `importance`, then the `ValidatedFact` store — last, because a store
   is only as good as the validation that fills it.

## Evidence base

### Provenance of this document

The findings above were assembled by nine prepared `agents-core/subagent/1`
research workers, one per field, each required to verify every named source
against a page or document retrieved in its own run and to list every citation it
could not verify. Four passed structural validation on the first attempt; five
failed on the plain-text result format only — a duplicate evidence reference
inside a single list item, and one missing ` — ` separator — and were repaired by
a second attempt that changed no content. All nine results validated before any
of their content entered this document. Validation remains
`verification: structural_only`, so the usual caveat applies in full: it
establishes shape, not truth.

One citation was re-verified by the author of this document, through a different
route than the worker used, because the whole of Layer 2 leans on it:
arXiv:2606.20158, "N-Version Programming with Coding Agents" (Ron, Baudry and
Monperrus, KTH and Université de Montréal, submitted 18 June 2026). A search of
the published record confirms the title, the three authors, the identifier, the
48 agent-generated implementations, the million-input campaign, the common-mode
finding, and the voting result quoted above. The workers' own PDF extraction and
this search agree, which is the convergent-methods condition the layer asks of
everything else. A closely related paper surfaced in the same search and is
recorded as an unread pointer: arXiv:2607.02808, "A Systematic Methodology for
Evaluating Failure Independence in LLM-Generated Code".

### What the research workers could not verify

Reported as limitations rather than silently dropped, and relevant to how much
weight each finding carries:

- Paywalled or blocked primary texts: Wald (1945), Wald and Wolfowitz (1948),
  Lindley (1956), Raiffa and Schlaifer (1961), Howard (1966), Pocock (1977),
  O'Brien and Fleming (1979), Weitzman (1979), Deming's *Out of the Crisis*, and
  the ISO standards' normative clause text. Each result attributed to them was
  verified against a retrieved source that states both the result and the
  citation; the originals' wording was not read.
- Goodhart's original 1975 wording is **not** verified; retrieved secondary
  sources give at least three different sentences. What is verified is that the
  universally quoted modern form is Strathern's own sentence from 1997.
- The heterogeneity numbers for arXiv:2502.08788 (76.7 against 72.1) come from a
  secondary overview page, not the paper's own tables, which could not be
  retrieved.
- Anthropic's multi-agent figures come from a company engineering report on an
  internal eval with no stated set size, variance or cost-matched baseline.
- One conformity source (arXiv:2608.11247) was verified only against its
  abstract page.
- Several standard results rest on university course notes rather than journal
  articles: Wald's inequalities and the expected-sample-size bound, the
  paired-comparison power formula, and the Neyman-Pearson lemma.

### Clusters and their full extractions

Nine clusters were researched: epistemology and severe testing; judgment
aggregation and truth discovery without ground truth; redundancy and correlated
failure; adversarial and dialectical procedures; scalable oversight in AI;
metrology and measurement assurance; provenance and attestation; sequential
stopping and value of information; and measured failures of multi-agent LLM
systems. Each returned between 16 and 30 conclusions with per-claim evidence
references. The extractions are not reproduced here; the citations that this
plan actually leans on are named inline above, and the key primary sources are:

Knight and Leveson, IEEE TSE 12(1):96-109 (1986) and Brilliant, Knight and
Leveson, IEEE TSE 16(2):238-247 (1990) · Ron, Baudry and Monperrus,
arXiv:2606.20158 (2026) · Eckhardt and Lee, IEEE TSE SE-11(12):1511-1517 (1985)
· Littlewood and Miller, IEEE TSE 15(12):1596-1614 (1989) · Clemen and Winkler,
Operations Research 33(2):427-442 (1985) · Ladha (1992) and List and Pettit,
Economics and Philosophy 18(1):89-110 (2002) · Nitzan and Paroush (1982) ·
Dawid and Skene, JRSS-C 28(1):20-28 (1979) · Parisi, Strino, Nadler and Kluger,
PNAS 111(4):1253-1258 (2014) · Jaffe et al., PMLR 51:351-360 (2016) · Prelec,
Science 306(5695):462-466 (2004) and Prelec, Seung and McCoy, Nature 541:532
(2017) · Miller, Resnick and Zeckhauser, Management Science 51(9):1359-1373
(2005) and Gao, Mao, Chen and Adams, EC 2014:507-524 · Gneiting and Raftery,
JASA 102(477):359-378 (2007) · Murphy, Journal of Applied Meteorology
12(4):595-600 (1973) · Kumar, Liang and Ma, NeurIPS 2019 · Feinstein and
Cicchetti, Journal of Clinical Epidemiology 43:543-549 (1990) ·
Bikhchandani, Hirshleifer and Welch, Journal of Political Economy
100(5):992-1026 (1992) · Mayo, *Statistical Inference as Severe Testing*,
Cambridge University Press (2018), doi:10.1017/9781107286184 · Toulmin, *The
Uses of Argument* (1958; CUP 2003), doi:10.1017/CBO9780511840005 · Pollock,
Cognitive Science 11(4):481-518 (1987) · Walton, Reed and Macagno,
*Argumentation Schemes*, CUP (2008) · Campbell and Fiske, Psychological Bulletin
56(2):81-105 (1959) · Cronbach and Meehl, Psychological Bulletin 52:281-302
(1955) · Goal Structuring Notation Community Standard v3, SCSC (4 May 2021) ·
Leveson, MIT ESD-WP-2011-13 (2011) · Greenwell, Knight, Holloway and Pease,
24th International System Safety Conference (2006) · Silberzahn et al. (2018),
Botvinik-Nezer et al. (2020) and Breznau et al. (2022) · Steegen et al. (2016)
and Simonsohn, Simmons and Nelson (2020) · Nosek, Ebersole, DeHaven and Mellor,
PNAS 115:2600-2606 (2018) and Scheel, Schijen and Lakens, AMPPS 4 (2021) ·
Open Science Collaboration, Science 349(6251):aac4716 (2015) · Schulz, Chalmers,
Hayes and Altman, JAMA 273(5):408-412 (1995) · Hróbjartsson et al. on blinded
outcome assessors · Cole, Cole and Simon (1981) · Mellers et al. (2014),
Atanasov et al. (2017), Hanea et al. (2017) and Rowe and Wright (1999) ·
Herzog and Hertwig (2009) · Dhami, Belton and Mandel, Applied Cognitive
Psychology 33:1080-1090 (2019) and Wilcox and Mandel (2023) · Irving,
Christiano and Amodei (2018), Khan et al. (2024), Kirchner et al. (2024),
Lightman et al., Wang et al. (2023), Huang et al. (ICLR 2024, arXiv:2310.01798),
Stechly et al. (arXiv:2310.12397), CriticBench (arXiv:2402.14809), Zheng et al.
(MT-Bench), Panickssery, Bowman and Feng (arXiv:2404.13076), Goel et al.
(arXiv:2502.04313), Sharma et al. (2023), Vovk and Angelopoulos and Bates on
conformal prediction · Cemri et al., arXiv:2503.13657 (MAST) · Du et al.
(arXiv:2305.14325), Liang et al. (arXiv:2305.19118), Wang et al.
(arXiv:2402.18272), Smit et al. (arXiv:2311.17371), Zhang et al.
(arXiv:2502.08788), Kapoor et al. (arXiv:2407.01502), Zhu et al.
(arXiv:2507.02825), Weng et al. (arXiv:2501.13381), Liu et al.
(arXiv:2307.03172), Meyerson et al. (arXiv:2511.09030), Prasad et al.
(arXiv:2311.05772) · JCGM 100:2008 (GUM), JCGM 200:2012 (VIM), JCGM 106:2012,
ILAC-G8:09/2019, UKAS LAB 48 Ed. 5 (2024), CIPM MRA-G-11 v1.2 (2025),
ISO/IEC 17025:2017, ISO/IEC 17043:2023, ISO 13528:2022, ISO 5725-1:2023,
ISO 17034:2016, ISO 7870-2:2023 · W3C PROV-DM (2013), in-toto v1.0.0 (2023),
SLSA v1.2, RFC 6962 and RFC 9162, NIST SP 800-86, MHRA GXP Data Integrity
Guidance Rev 1 (2018), 21 CFR 11.10, Thompson, CACM 27(8):761-763 (1984),
Wheeler on diverse double-compiling, Wilkinson et al., Scientific Data 3:160018
(2016), NASEM (2019) doi:10.17226/25303, Greshake et al. (arXiv:2302.12173) ·
Campbell, JMDE 7(15):3-43 (2011 reprint of the 1976 paper) and Strathern,
European Review 5(3):305-321 (1997) · Sijtsma, Psychometrika 74(1):107-120
(2009).
