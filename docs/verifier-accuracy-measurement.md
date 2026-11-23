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
slower, and the replacement rule below needs it before any value is raised.

One verdict per item measures only a route's single-verdict accuracy
`a = c·b + (1 − c)·(1 − b)`. The split between `c` and `b` is how often a family's
verdicts fail together on the same claim, so every item gets two verdicts of the same
route: two V3 for the model family, and two V2 for the execution family on the items
a command can decide.

| Measured | Prior | Kept as a prior, and why |
|---|---|---|
| `c_model`, `b_V3` | 0.97, 0.97 | `b_V1`: production V1 is the parent's own source check, which a fresh worker does not reproduce and which cannot be blinded; whether V1 shares the model family stays an assumption |
| `c_execution`, `b_V2` | 0.98, 0.95 | `c_owner`, `b_V4`: a blind test of the owner's acceptance would need planted defects merged under the owner's eyes |
| sensitivity and specificity per route; whether the families fail independently | (assumed symmetric and independent) | V3 verdicts from review bots and humans, which are other configurations; `VOLATILITY`, a property of claims, not of verifiers: items pinned to a commit have none, so it needs re-checks of stored claims at random ages |

**What the estimates cover.** Claims about one pinned snapshot of this repository, in
the strata below. Applying them to other kinds of claims (external URLs, citations,
the behaviour of other services) is an assumption, which the production audit tests;
this is one reason a raise waits for that audit.

## The control set

- **120 pairs** at one pinned commit of this repository, each a true statement and a
  false counterpart that differs by one perturbation. At least 100 pairs are decidable
  by running something (the V2 subset).
- **Strata** are the `no-fabrication` claim types a repository snapshot can decide:
  names and signatures, CLI flags, configuration keys and defaults, paths, numeric
  constants, quoted documentation text, pinned versions. Estimates are reported per
  stratum as well as pooled.
- **Perturbations** copy real errors: a near-miss name, an off-by-one or wrong-unit
  number, a wrong default, a path one segment off, a negated condition, and a value that
  was true at an earlier commit. Subjects are drawn from real recorded claims where
  there are any, so that item difficulty resembles production.
- **Labels** are fixed before any verifier runs: truth by construction, confirmed by a
  deterministic check at the snapshot where one exists and by the owner otherwise. No
  label changes after verdicts are seen. An item found defective later is reported, and
  the analysis is shown with and without it: relabelling only where verifiers
  disagreed with the label would bias accuracy upward.
- **Storage** is a `claims-control/` directory (0700/0600) under the service-state
  directory (`state_dir()` in [state.py](../src/daemon/state.py)), outside every
  checkout. The installation's `data/` directory would not do: it sits inside the
  installation tree. The first 16 hexadecimal characters of the item file's SHA-256
  name the set version. The set enters no prompt, rule, skill, eval example or claim
  store: the plan's "keep one unused channel".
- **Blinding is checked, not assumed.** Each worker's read scope is the snapshot
  worktree, and the runner searches each worker transcript for any path under the
  control directory; a run that touched it is discarded and counted.
- **A 10-pair pilot**, discarded afterwards, checks the runner, the blinding and the
  cost of a run.

## The runs

Each run is one item, one route and one fresh worker through the normal exchange:
`get_subagent_context`, a native worker, `validate_subagent_result`. The package looks
like a production verification package and holds the statement and a read-only
worktree at the snapshot, never the answer. The two members of a pair never share a run.

- **V3 arm:** two independent blinded verifiers per item, given the statement as a
  neutral question.
- **V2 arm:** two independent verifiers per item of the execution subset, required to
  show the command and its output.

A verdict's level is the one its validated evidence earns under the
[thread-close](../flows/thread-close.md) table, not the arm's name: a V2 run that only
read a file is a V1 verdict, recorded with its arm. The fit uses only verdicts whose
earned level matches their arm; the others are counted and reported. Accuracy is
conditional on a
verdict, as in production, where only `confirmed` and `refuted` reach the store;
abstention, invalid and unfinished rates are reported per class.

The configuration is the client, tier, resolved model, reasoning effort and package
`bundle_revision`. The resolved model is the provider's model ID read from the worker
transcript; a run whose transcript does not name it is excluded and counted, never
filed under the tier's alias, because an alias such as `sonnet` moves between
snapshots. The first run measures the `claude` client at the tiers verification uses
today: 240 items × 2 V3 runs + 200 items × 2 V2 runs = **880 runs**, plus about 80 for
the pilot.

## Recording path

The runner, which alone holds the answers, appends one line per scheduled attempt
through `src/engine/subagent_ledger.py`, under its opt-in and privacy shape (no
statement text, hashed task IDs), but to its own file
`data/subagent-tiers/control.jsonl`. The main ledger rotates past 5 MiB by replacing
`ledger.jsonl.1`, so a second rotation would lose control lines; a full run is under
1 MB and its file is not rotated.

```json
{"at": "2026-12-01T10:00:00+00:00", "event": "control_attempt",
 "set": "<set id>@<16 hex>", "pair": "p017", "item": "p017f", "stratum": "config_default",
 "truth": false, "arm": "V3", "slot": 2, "status": "verdict",
 "level": "V3", "family": "model", "verdict": "confirmed", "outcome": "FN",
 "task": "<hashed task id>", "attempt_id": "a1", "contract_revision": "<64 hex>",
 "client": "claude", "tier": "standard", "model": "<resolved provider model ID>",
 "reasoning_effort": "<effort>", "bundle_revision": "<64 hex>"}
```

- `arm` and `slot` are the scheduled run (`V3` or `V2`, slot 1 or 2), independent of
  the `level` the evidence earned, so a missing planned verdict and outcomes by arm can
  be read off the file.
- `status` is `verdict`, `abstained` (the verifier could not tell), `invalid` (the
  result failed validation) or `unfinished` (no result within the runner's time limit,
  written when the runner gives up). Only `verdict` lines carry `level`, `verdict` and
  `outcome`; every status is counted per class, and an `unfinished` attempt is reported
  as such, never folded into the other three.

The positive class, defined once as the plan requires, is "the verifier says the claim
is false". `TP` is a false item refuted, `FN` a false item confirmed (a false accept),
`TN` a true item confirmed and `FP` a true item refuted; sensitivity is
`TP / (TP + FN)` and specificity `TN / (TN + FP)`.

## Analysis

`c` and `b` are fitted by maximum likelihood of model.py's own model: per item the
family reading is right with probability `c`, and each verdict agrees with the reading
with probability `b`. The fit runs on all items and separately on true and false items.
Bands are percentile bootstraps over pairs (2000 resamples), which keeps both the
within-item correlation the model is about and the source a pair shares.

**Simultaneous bands.** The replacement rule acts on four parameters (`c_model`,
`b_V3`, `c_execution`, `b_V2`), so their bands are simultaneous: each at
1 − 0.05/4, z = 2.50, for 95 % jointly. The two checks below and the per-stratum
estimates produce findings for the owner, not replacements, and use plain 95 % bands.

Two checks can return a finding instead of a number, and each finding needs a code
change with its own approval:

- **Symmetry.** model.py gives one accuracy to confirming a true claim and to refuting a
  false one. If the band of specificity minus sensitivity of a route excludes zero, the
  symmetric model misweights a confirmation: specificity 0.98 with sensitivity 0.80
  gives a confirmation a likelihood ratio of 4.9, where the symmetric model at their
  mean of 0.89 gives 8.1.
- **Independent families.** model.py assumes the model and execution families fail
  independently given the truth. The items of the execution subset carry two verdicts
  of each family, so coincident V3 and V2 failures can be counted against the
  independence prediction. An excess means one family's confirmation is worth less
  after the other's than the filter assumes.

The same data gives layer 3 its ρ, and a stratum whose band lies wholly below the
pooled one is reported as a finding about transport.

## Sample size

The numbers come from the Fisher information of model.py's likelihood at the priors,
checked by simulating 300 sets of 200 items (standard deviation of the fitted
`c_model` 0.0131 simulated against 0.0126 predicted, for a design with one more
verdict per item than this one).

**Items, not runs, are the bottleneck.** Even with every item's family reading known
exactly, `c_model` at 0.97 is a binomial that needs 202 items for a simultaneous band
of ±0.03. Two V3 verdicts per item come close to that (222 items); a third would add
little.

Half-widths of the simultaneous bands, at the priors and at lower true values
(`c_model` 0.93, `b_V3` 0.93, `c_execution` 0.95, `b_V2` 0.90):

| Parameter | What it decides | Items | At the priors | Lower truth |
|---|---|---|---|---|
| `c_model` | whether one model family can carry a `critical` claim (0.95) | 240 | ±0.029 | ±0.046 |
| `b_V3` | one V3's weight (`a` = 0.942) | 240 | ±0.020 | ±0.032 |
| `c_execution` | the execution family's cap | 200 | ±0.028 | ±0.049 |
| `b_V2` | one V2 against `load_bearing` 0.85 (`a` = 0.932) | 200 | ±0.029 | ±0.042 |

Meeting a width of ±0.03 at the priors takes 111 pairs for the model family and 92
command-checkable pairs; 120 and 100 leave a small margin. When the true values are
lower, the bands widen and stay above ±0.03: the rule then keeps a prior unless the
band lies wholly below it.

Per class, with one verdict per item, the plain 95 % bands are ±0.042 for V3 on 120
items and ±0.049 for V2 on 100: the symmetry check catches a gap of about 0.1 or more,
such as the example above, and not a gap of 0.03.

What the set cannot do: decide `c_model` against 0.95 when it lies near it. With 240
items the simultaneous band excludes 0.95 only for an estimate below about 0.89 or
above about 0.98, and settling 0.97 against 0.95 would take several hundred more items.
In between the prior stays and the report says the question is open.

The set runs in two halves of 60 pairs. The look after the first half applies only
rule 3 below, at z = 3.29 (p < 0.001) per parameter, so the final look keeps the joint
level close to 95 %.

## Replacement rule

Two preconditions come first. Each stored verdict must name the configuration that
produced it, a new optional field of `claim_record`: a measured value then applies
only to verdicts recorded under the configuration it was measured on, and every other
verdict, including all verdicts recorded before the field existed, keeps the priors.
And the bands are the simultaneous ones above.

A parameter is **narrow** when its simultaneous band's half-width is at most 0.03.
Per parameter, with prior `θ0`, estimate `θ̂` and band `[L, U]`:

1. Narrow and `θ̂ ≤ θ0`: use `θ̂`.
2. Narrow and `θ̂ > θ0`: keep `θ0` until the random production audit agrees with the
   control set for that route; then use `max(θ0, L)`. A raise goes only as far as the
   evidence guarantees, the rule validated-claims.md already states: an adaptive
   estimate must never loosen a safety bound. This note does not design the audit.
3. Not narrow, but `U < θ0`: the prior is refuted; use `U`, the smallest tightening the
   data forces.
4. Otherwise keep `θ0` and report the parameter as unmeasured at this precision.

Lowering a value is not safe in every history: with mixed verdicts, a weaker
refutation can leave a claim more confident than before. That is why even rules 1 and
3 apply only to verdicts of the measured configuration.

Values change by a reviewed commit to model.py that carries a results document: each
value with its band, the set version, the configuration, the counts and, for a raise,
the audit result. Nothing reads the control file at run time. Because the store
recomputes every state on read, the new values re-read every stored verdict of the
measured configuration.

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

- **P1.** For both routes, sensitivity is below specificity: verifiers lean towards
  confirming.
- **P2.** The families are not independent given the truth: V3 and V2 fail together
  on the same items more often than independence predicts.
- **P3.** `c_model` is below its prior of 0.97.

## What approval covers

Building the set (130 pairs with the pilot, and the owner's review of labels no
deterministic check decides), a runner (a flow and a script under `evals/`, with no
answers in the repository), the `control.jsonl` recording, about 960 worker runs, the
configuration field on `claim_record`, a results document, and a later commit that
applies the replacement rule. Not covered: the production audit that a raise needs,
asymmetric accuracies or a dependence term if the checks call for them, `VOLATILITY`,
V1, V4, and bot and human V3.
