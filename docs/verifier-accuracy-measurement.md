# Measuring verifier accuracy with a known-answer control set

**Status:** proposal, 2026-11-23, awaiting the owner's approval; nothing is implemented.
It designs steps 6 to 8 of the
[reliability plan's order of work](delegation-reliability-plan.md#order-of-work)
(the measurement its layer 6 describes) for the parameters of
[validated claims](validated-claims.md).

## What is measured, and why only this way

The claim filter in [model.py](../src/claims/model.py) runs on two kinds of numbers,
both priors today: `FAMILIES`, the common-mode accuracy `c` of each family of routes,
and `ROUTES`, each level's own accuracy `b`. Replacing them needs verdicts judged
against an answer that is known before the verdict exists. The two labels already at
hand are biased: a label taken from the store is the verifiers' own output, so it is
circular, and a label from a resolved contradiction exists only where verifiers
disagreed, a selection that makes them look worse than they are. A random audit of
production claims ending in a reliable label is the other unbiased source; it is
slower, and it is the later check that the control set resembles real claims.

One verdict per item measures only a route's single-verdict accuracy
`a = c·b + (1 − c)·(1 − b)`. The split between `c` and `b` is how often a family's
verdicts fail together on the same claim, so every item gets at least two verdicts of
the same family: one V1 and two V3 for the model family, two V2 for the execution family.

| Measured | Prior | Kept as a prior, and why |
|---|---|---|
| `c_model`, `b_V1`, `b_V3` | 0.97, 0.85, 0.97 | `c_owner`, `b_V4`: a blind test of the owner's acceptance would need planted defects merged under the owner's eyes |
| `c_execution`, `b_V2` | 0.98, 0.95 | V3 verdicts from review bots and humans: other configurations than the worker this run measures |
| sensitivity and specificity per route; the family assignment | (assumed symmetric; V1 and V3 share a family) | `VOLATILITY`: a property of claims, not of verifiers; items pinned to a commit have none, so it needs re-checks of stored claims at random ages, a different design |

## The control set

- **120 pairs** at one pinned commit of this repository, each a true statement and a
  false counterpart that differs by one perturbation. At least 60 pairs are decidable
  by running something (the V2 subset).
- **Strata** are the `no-fabrication` claim types a repository snapshot can decide:
  names and signatures, CLI flags, configuration keys and defaults, paths, numeric
  constants, quoted documentation text, pinned versions.
- **Perturbations** copy real errors: a near-miss name, an off-by-one or wrong-unit
  number, a wrong default, a path one segment off, a negated condition, and a value that
  was true at an earlier commit. Subjects are drawn from real recorded claims where
  there are any, so that item difficulty resembles production.
- **Labels** are fixed before any verifier runs: truth by construction, confirmed by a
  deterministic check at the snapshot where one exists and by the owner otherwise. No
  label changes after verdicts are seen. An item found defective later is reported, and
  the analysis is shown with and without it: relabelling only where verifiers
  disagreed with the label would bias accuracy upward.
- **Storage** is the installation's private data directory, `data/claims-control/`
  (0700/0600), outside the repository; the snapshot commit predates the set, and the
  first 16 hexadecimal characters of the item file's SHA-256 name the set version. The
  set enters no prompt, rule, skill, eval example or claim store: the plan's
  "keep one unused channel".
- **A 10-pair pilot**, discarded afterwards, checks the runner, the blinding and the
  cost of a run.

## The runs

Each run is one item, one route and one fresh worker through the normal exchange:
`get_subagent_context`, a native worker, `validate_subagent_result`. The package looks
like a production verification package and holds the statement and a read-only
worktree at the snapshot, never the answer. The two members of a pair never share a run.

- **V1 arm:** the check as a parent does it today, with the statement given as the
  working belief and one source opened. It is a stand-in: production V1 is mostly the
  parent itself, which cannot be blinded without filling its context with the set.
- **V3 arm:** two independent blinded verifiers, given the statement as a neutral
  question.
- **V2 arm:** two independent verifiers on the execution subset, required to show the
  command and its output.

A verdict's level is the one its validated evidence earns under the
[thread-close](../flows/thread-close.md) table, not the arm's name: a V2 run that only
read a file is a V1 verdict. A structurally invalid result is not a verdict and is
counted apart. A verifier that cannot tell records `abstained`; accuracy is
conditional on a verdict, as in production, where only `confirmed` and `refuted`
reach the store, and abstention rates are reported per class.

The configuration is the client, tier, resolved model (from the worker transcript
where it names one, the tier's alias otherwise), reasoning effort and package
`bundle_revision`. The first run measures the `claude` client at the tiers verification
uses today: 240 items × 3 model-family runs + 120 items × 2 execution runs = **960 runs**,
plus about 100 for the pilot.

## Recording path

The runner, which alone holds the answers, appends one line per validated verdict
through `src/engine/subagent_ledger.py`, under its opt-in and privacy shape (no
statement text, hashed task IDs), but to its own file
`data/subagent-tiers/control.jsonl`. The main ledger rotates past 5 MiB by replacing
`ledger.jsonl.1`, so a second rotation would lose control lines; a full run is under
1 MB and its file is not rotated.

```json
{"at": "2026-12-01T10:00:00+00:00", "event": "control_verdict",
 "set": "<set id>@<16 hex>", "pair": "p017", "item": "p017f", "stratum": "config_default",
 "truth": false, "level": "V3", "family": "model", "verdict": "confirmed", "outcome": "FN",
 "task": "<hashed task id>", "attempt_id": "a1", "contract_revision": "<64 hex>",
 "client": "claude", "tier": "standard", "model": "<resolved model or alias>",
 "reasoning_effort": "<effort>", "bundle_revision": "<64 hex>"}
```

The positive class, defined once as the plan requires, is "the verifier says the claim
is false". `TP` is a false item refuted, `FN` a false item confirmed (a false accept),
`TN` a true item confirmed and `FP` a true item refuted; sensitivity is
`TP / (TP + FN)` and specificity `TN / (TN + FP)`.

## Analysis

`c` and `b` are fitted by maximum likelihood of model.py's own model: per item the
family reading is right with probability `c`, and each verdict agrees with the reading
with probability `b`. The fit runs on all items and separately on true and false items.
Bands are 95 % percentile bootstraps over pairs (2000 resamples), which keeps both the
within-item correlation the model is about and the source a pair shares.

Two checks can return a finding instead of a number, and each finding needs a code
change with its own approval:

- **Symmetry.** model.py gives one accuracy to confirming a true claim and to refuting a
  false one. If the band of specificity minus sensitivity of a route excludes zero, the
  symmetric model misweights a confirmation: specificity 0.98 with sensitivity 0.80
  gives a confirmation a likelihood ratio of 4.9, where the symmetric model at their
  mean of 0.89 gives 8.1.
- **Family assignment.** Three model verdicts per item give eight outcome patterns,
  which leave four degrees of freedom after `c`, `b_V1` and `b_V3`. A G² test on them
  (p by parametric bootstrap) that rejects says V1 and V3 do not fail together as one
  shared reading predicts: the family table is wrong, not only its numbers.

The same data gives layer 3 its ρ: coincident V1–V3 and V3–V3 failures against the
independence prediction.

## Sample size

The numbers come from the Fisher information of model.py's likelihood at the priors,
checked by simulating 300 sets of 200 items (standard deviation of the fitted
`c_model` 0.0131 simulated against 0.0126 predicted).

**Items, not runs, are the bottleneck.** Even with every item's family reading known
exactly, `c_model` at 0.97 is a binomial that needs 125 items for a band of ±0.03. Two
V3 verdicts per item come close to that (137 items), a third adds little (131), and the
single V1 verdict is the noisy route: `b_V1` needs 139 items for ±0.06.

Half-widths of the 95 % band, at the priors and at lower true values (`c_model` 0.93,
`b_V1` 0.80, `b_V3` 0.93, `c_execution` 0.95, `b_V2` 0.90):

| Parameter | What it decides | 140 / 70 items | 240 / 120 items | Lower truth, 240 / 120 |
|---|---|---|---|---|
| `c_model` | whether one model family can carry a `critical` claim (0.95) | ±0.029 | ±0.023 | ±0.036 |
| `b_V3` | one V3's weight (`a` = 0.942) | ±0.021 | ±0.016 | ±0.024 |
| `b_V1` | one V1 against `routine` 0.75 (`a` = 0.829) | ±0.060 | ±0.046 | ±0.053 |
| `c_execution` | the execution family's cap | ±0.037 | ±0.028 | ±0.050 |
| `b_V2` | one V2 against `load_bearing` 0.85 (`a` = 0.932) | ±0.038 | ±0.029 | ±0.043 |

The first count is model-family items, the second execution items. Meeting every
width below at the priors takes about 70 model pairs and 57 execution pairs; 120 and
60 leave room for true values below the priors, which widen every band.

Per class, with one verdict per item, the bands are ±0.042 for V3 and ±0.067 for V1 on
120 items and ±0.064 for V2 on 60: the symmetry check catches a gap of about 0.1 or
more, such as the example above, and not a gap of 0.03.

What the set cannot do: decide `c_model` against 0.95 when it lies between about 0.92
and 0.97. That needs about 450 items for 80 % power at 0.97; 240 items settle it only
for an estimate below about 0.91 or above about 0.975. In between the prior stays and
the report says the question is open.

The set runs in two halves of 60 pairs. The look after the first half applies only
rule 3 below, at z = 3.29 (p < 0.001), so the final look keeps almost all of its
nominal 95 %.

## Replacement rule

A parameter is **narrow** when its band's half-width is at most 0.03 (`c_model`,
`c_execution`, `b_V2`, `b_V3`) or 0.06 (`b_V1`, which decides only the `routine`
bound, 0.08 below its prior). Per parameter, with prior `θ0`, estimate `θ̂` and band
`[L, U]`:

1. Narrow and `θ̂ ≤ θ0`: use `θ̂`.
2. Narrow and `θ̂ > θ0`: use `max(θ0, L)`. A raise goes only as far as the evidence
   guarantees, the rule validated-claims.md already states: an adaptive estimate must
   never loosen a safety bound.
3. Not narrow, but `U < θ0`: the prior is refuted; use `U`, the smallest tightening the
   data forces.
4. Otherwise keep `θ0` and report the parameter as unmeasured at this precision.

Values change by a reviewed commit to model.py that carries a results document: each
value with its band, the set version, the configuration and the counts. Nothing reads
the control file at run time. Because the store recomputes every state on read, the
new values re-read every stored verdict.

## After the first run: configurations and drift

Verifier accuracy changes mostly in steps at known moments, a new model snapshot or a
new verifier package, and the estimate for a step is one per configuration, pooled
without fading. Fading memory forgets on a clock instead: holding a strength of 150
outcomes at a 90-day half-life takes about 35 control verdicts per route each month
(rate `n · ln 2 / H`), indefinitely, for a band the pooled estimate keeps for free. So a
new model snapshot on a route gets a full run, and the constants come from
per-configuration estimates.

Fading memory has one job. The Claude tiers name aliases (`haiku`, `sonnet` in
[model_tiers.py](../src/engine/model_tiers.py)), so a route's model can change without
a recorded event. A 20-pair sentinel, quarterly and on every verifier package change,
feeds [rate.py](../src/estimation/rate.py) per route and class with a 180-day
half-life, which accumulates evidence across sentinels as an EWMA chart does; a band
wholly below the configuration's estimate triggers a full run. One sentinel catches a
collapse, not a slide: its 80 V3 verdicts alarm at 12 errors (one-sided 0.005), which
detects `a_V3` falling from 0.942 to 0.80 with power 0.90 and to 0.85 with power 0.55,
counted as if independent, so these powers are optimistic. This phase is not part of
the first approval.

## Predictions, stated before the run

These are the author's predictions, recorded so that the run can falsify them:

- **P1.** For every model-family route, sensitivity is below specificity: verifiers
  lean towards confirming.
- **P2.** The family assignment holds: V1 and V3 errors are positively correlated.
- **P3.** `c_model` is below its prior of 0.97.

## What approval covers

Building the set (130 pairs with the pilot, and the owner's review of labels no
deterministic check decides), a runner (a flow and a script under `evals/`, with no
answers in the repository), the `control.jsonl` recording, about 1060 worker runs, a
results document, and a later commit that applies the replacement rule. Not covered:
asymmetric accuracies or a changed family table if the checks call for them,
`VOLATILITY`, V4, bot and human V3, and recording a verifier's configuration on each
stored verdict so that old verdicts keep the calibration they were made under.
