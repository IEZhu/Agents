# Measuring verifier accuracy with a known-answer control set

**Status:** merged 2026-11-23. The first stage is implemented: the builder, the
runner with its blinding, the recorder and the analysis are in
[evals/verifier_accuracy](../evals/verifier_accuracy/__init__.py), driven by the
[verifier accuracy flow](../flows/verifier-accuracy.md). The 20-item pilot ran on
2026-11-23; its [report](records/verifier-accuracy-pilot.md) gives the cost per run, the
blinding result and what in this design proved unworkable. The full run went in
stages the owner approved one at a time: the first, 206 of its 890 runs, ran on
2026-11-24, and its [report](records/verifier-accuracy-stage1.md) gives the projection of
the whole set. On 2026-11-26 the owner stopped after that stage: with every verdict
right so far, a run with no wrong verdict would move no prior under the size-weighted
fallback. No value is applied to the claim filter. The set stays in the control
directory, so a later stage could still continue it with the same seed, 0.
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

- **240 items** at one pinned commit of this repository, each from a different source
  file: 120 true statements and 120 false ones. A false item is one perturbation of a
  true statement that is not itself in the set. At least 100 items of each class are
  decidable by running something (the V2 subset). Different files are not independent
  sources, since files share conventions and a verifier can repeat one mistake across
  them, so the analysis treats each source directory two levels deep (for example
  `src/engine`) as a cluster.
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
- **Storage** is a control directory (0700/0600) under the service-state directory
  (`state_dir()` in [state.py](../src/daemon/state.py)), holding the statements, the
  labels and the attempt records in separate files. The installation's `data/`
  directory would not do: it sits inside the installation tree. Because
  `AGENTS_SERVICE_DIR` can point anywhere, the runner refuses to start when the
  resolved directory lies inside the installation root or inside any checkout a worker
  is given. The first 16 hexadecimal characters of the statement file's SHA-256 name
  the set version. The set enters no prompt, rule, skill, eval example or claim store:
  the plan's "keep one unused channel".
- **Blinding is enforced and tested.** A package's `scope` is an instruction, not a
  sandbox, so the runner launches every worker with the control directory denied at
  the host or operating-system level, for file tools and shell alike. Before each
  batch a probe worker tries to read a canary file in that directory; if the read
  succeeds, the run stops. Searching each worker transcript for the directory's path
  is kept as a second check, and a run that touched it is discarded and counted.
- **A 20-item pilot**, discarded afterwards, checks the runner, the blinding and the
  cost of a run.

## The runs

Each run is one item, one route and one fresh worker through the normal exchange:
`get_subagent_context`, a native worker, `validate_subagent_result`. The package looks
like a production verification package and holds the statement and a read-only
worktree at the snapshot, never the answer, and a run sees exactly one item.

- **V3 arm:** two independent blinded verifiers per item, given the statement as a
  neutral question.
- **V2 arm:** two independent verifiers per item of the execution subset, required to
  show the command and its output.

`validate_subagent_result` checks structure only, and a worker's account of its own
evidence is narration, so the runner assigns each level from what the host recorded,
in the spirit of the [thread-close](../flows/thread-close.md) table, and never re-runs
anything a worker wrote:

- **V3** is a verdict of the V3 arm, fresh and blinded by construction, whose
  transcript records at least one read of a file in the snapshot worktree. A V3-arm
  verdict without one is counted and reported as unsupported, never fitted.
- **V2** is a verdict of the V2 arm whose transcript records at least one shell or tool
  execution by the worker. A V2-arm verdict without one is counted and reported as V1,
  never fitted.

Whether a verdict's evidence actually supports it is not adjudicated, because that is
what accuracy measures: a verdict resting on an unrelated command is scored by whether
it is right, just as production weights a V2 verdict that the parent accepted on the
same evidence. Filtering such verdicts out would make the routes look better than they
are in use. Accuracy is
conditional on a verdict, as in production, where only `confirmed` and `refuted` reach
the store; abstention, invalid and unfinished rates are reported per class.

The configuration is the client, tier, resolved model, reasoning effort and package
`bundle_revision`. The resolved model is the provider's model ID read from the worker
transcript; a run whose transcript does not name it is excluded and counted, never
filed under the tier's alias, because an alias such as `sonnet` moves between
snapshots. The first run measures the `claude` client at the tiers verification uses
today: 240 items × 2 V3 runs + 200 items × 2 V2 runs = **880 runs**, plus about 80 for
the pilot. These runs measure one configuration; every further configuration the
routes use, another tier or client, needs its own full run, and until it has one its
verdicts keep the priors.

**Schedule and stages.** The runner orders the set's clusters by one seed and keeps every
slot of a cluster's items together. The seed belongs to the set: every attempt record
names it, and a later run with another seed is refused, so the stages together are
always the leading clusters of one order. A stage of `n` runs takes the leading
`n × clusters // slots` clusters, the count the budget holds on average, so every cluster
has the same chance to be in it and the stage is a simple random sample of clusters
whose design effect estimates the whole set's. Cutting instead where the leading clusters
stop fitting `n` would leave out the cluster that crosses the limit, more often a large
one, and would never take a cluster with more slots than `n`. The price is that a
stage's slots vary with the clusters drawn and can exceed `n`; `--max-total-usd` still
caps the spend. [Stage 1](records/verifier-accuracy-stage1.md) was cut the old way, and
its records predate the seed field: a later run continues them only with seed 0, the seed
they ran with, and only when they hold exactly the leading clusters of its order. One
command that writes a set goes at a time: `run`, `probe`, `discard` and `build` take the
set's run lock before they read the set and hold it until they end, and each is refused
while another holds it. A set a run or a probe loaded is therefore the one on disk until
it finishes, with no check afterwards. Before they create the lock file, `run` and `probe`
check, writing nothing, that the directory holds a set and lies apart from the installation
and the checkouts, so a refused command leaves no lock file behind. `Runner.run` refuses a set that was not loaded
under a run lock it still holds, so a caller outside the command line takes the lock
first too, and it counts done slots only from records of its own set.

## Recording path

The runner, which alone holds the labels, writes one line per scheduled attempt to
`attempts.jsonl` in the control directory through a recorder of its own. It keeps the
subagent ledger's privacy shape (no statement text, opaque item IDs, hashed task IDs)
but not its opt-in: the ledger drops lines while `AGENTS_SUBAGENT_LEDGER` is unset and
lives in the installation's `data/`. The control recorder always writes, never falls
back to the ledger's path, and stops the run on a failed write, because a missing
attempt would bias the completed set.

```json
{"at": "2026-12-01T10:00:00+00:00", "event": "control_attempt",
 "set": "<set id>@<16 hex>", "item": "i117", "stratum": "config_default",
 "truth": false, "arm": "V3", "slot": 2, "seed": 0, "status": "verdict",
 "level": "V3", "family": "model", "verdict": "confirmed", "outcome": "FN",
 "task": "<hashed task id>", "attempt_id": "a1", "contract_revision": "<64 hex>",
 "client": "claude", "tier": "standard", "model": "<resolved provider model ID>",
 "reasoning_effort": "<effort>", "bundle_revision": "<64 hex>"}
```

- `arm` and `slot` are the scheduled run (`V3` or `V2`, slot 1 or 2), independent of
  `level`, the level the runner assigned, so a missing planned verdict and outcomes by
  arm can be read off the file.
- `seed` is the schedule seed of the run that wrote the line, the same on every line of
  a set; the interim look reads it to find the first half.
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
with probability `b`. The fit runs separately for each configuration, on all items and
separately on true and false items. An item whose two verdicts name different
configurations is left out of the fit; the report gives that count and the items
remaining per configuration.

**Bands.** One construction gives every band the rule uses. For each family, the
posterior of `c` and `b` is computed on a grid from model.py's likelihood, with the
log-likelihood divided by the design effect `D` and uniform priors: over (0.5, 1) for
`c`, which settles the model's symmetry between `(c, b)` and `(1 − c, 1 − b)`, and over
(0, 1) for `b`. With both above 1/2, a single verdict's accuracy
`1/2 + 2·(c − 1/2)·(b − 1/2)` could never fall below 1/2, so a route worse than chance
could not show; with `b` free it shows as `b` below 1/2. A route whose band for `b`
lies wholly below 1/2 is reported as a finding (the plan's finding 3: below chance,
more verdicts make things worse) and never proposed as a value. A
parameter's band runs between two quantiles of its marginal posterior: 0.00625 and
0.99375 at the final look, Bonferroni over the four parameters the rule acts on
(`c_model`, `b_V3`, `c_execution`, `b_V2`), and 0.0005 and 0.9995 at the interim
look, which uses the first half's data only. The posterior keeps a band at the
boundary: when every verdict agrees, its upper edge is 1 and its lower edge lies below
it, which is the edge a raise uses.

`D` is the variance of the point estimates when source clusters are resampled, over
their variance when items are resampled, and never below 1. When either variance is
zero, as when every verdict agrees, `D` is the size-weighted mean cluster size, the sum
of squared cluster sizes over the item count: the design effect when a cluster's items
fail together, which for clusters of unequal size exceeds their plain mean. On the
240-item set, a run with no wrong verdict would then leave every prior where it is
([stage 1 report](records/verifier-accuracy-stage1.md#projection-of-the-whole-set)).

Before the run, the runner simulates this whole analysis at the priors and at
`b` = 0.995 and raises `D` until every band holds its level. The sample sizes below
plan with the normal approximation of these bands, z = 2.50 (3.29 at the interim). The
two checks below and the per-stratum estimates produce findings for the owner, not
replacements, and use plain 95 % bands.

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
verdict per item than this one). The Fisher information counts independent items.
With a design effect `D` the set carries the information of 240 / `D` items and every
width below grows by √`D`. The figures assume `D` = 1; the report states the measured
`D`, and a band it leaves wider than ±0.03 keeps its prior under rule 4.

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

Meeting a width of ±0.03 at the priors takes 222 items for the model family and 184
command-checkable items; 240 and 200 (100 of each class) leave a small margin at
`D` = 1. When the true values are
lower, the bands widen and stay above ±0.03: the rule then keeps a prior unless the
band lies wholly below it.

Per class, with one verdict per item, the plain 95 % bands are ±0.042 for V3 on 120
items and ±0.049 for V2 on 100: the symmetry check catches a gap of about 0.1 or more,
such as the example above, and not a gap of 0.03.

What the set cannot do: decide `c_model` against 0.95 when it lies near it. With 240
items the simultaneous band excludes 0.95 only for an estimate below about 0.89 or
above about 0.98, and settling 0.97 against 0.95 would take several hundred more items.
In between the prior stays and the report says the question is open.

The set runs in two halves of about 120 items. The first half is the stage of half the
set's slots under the seed the records name: the leading clusters, as many as half the
slots hold on average. The look after it fits that half's slots alone and applies only
rule 3 below, at its stricter quantiles, so the final look keeps the joint level close
to 95 %.

## Replacement rule

This note's approval covers measuring and reporting. Applying a value to the store is
a separate code change with its own approval, because today `Claim.verdicts()` builds
each verdict without metadata and `assess()` reads one global `FAMILIES` and
`ROUTES`. That change has to meet three requirements:

- **Per-verdict configuration.** A new optional `claim_record` field names the
  configuration that produced a verdict. A verdict uses its configuration's measured
  `b` where one exists and the prior otherwise, and every verdict recorded before the
  field existed keeps the priors. A family reading uses the lowest `c` among the
  configurations of that family's verdicts on the claim, with the prior standing in
  for a verdict that has no measured configuration.
- **A safety diff.** Before the commit, every stored claim is assessed under the old
  and the new values, and every claim whose `usable_for` grows is listed in the results
  document; the change waits for the owner to accept that list. A lower value does not
  guarantee a tighter result: with mixed verdicts, a weaker refutation can leave a
  claim more confident, so no direction of change is safe by itself.
- **The value proposed** for each parameter follows the four rules below.

A parameter is **narrow** when its simultaneous band's half-width is at most 0.03.
Per parameter, with prior `θ0`, estimate `θ̂` and band `[L, U]`:

1. Narrow and `θ̂ ≤ θ0`: propose `θ̂`.
2. Narrow and `θ̂ > θ0`: keep `θ0` until the random production audit agrees with the
   control set for that route; then propose `max(θ0, L)`. A raise goes only as far as
   the evidence guarantees, the rule validated-claims.md already states: an adaptive
   estimate must never loosen a safety bound. This note does not design the audit.
3. Not narrow, but `U < θ0`: the prior is refuted; propose `U`, the smallest
   tightening the data forces.
4. Otherwise keep `θ0` and report the parameter as unmeasured at this precision.

The results document carries each value with its band, the set version, the
configuration, the counts, the safety diff and, for a raise, the audit result. Nothing
reads the control directory at run time.

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
a recorded event. A 40-item sentinel, quarterly and on every verifier package change,
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

Building the set (260 items with the pilot, and the owner's review of labels no
deterministic check decides), a runner (a flow and a script under `evals/`, with no
labels in the repository) with its blinding enforcement and canary probe, the control
recorder, about 960 worker runs and a results document. Not covered: the change that
applies values to the store (per-verdict configuration, the lookup and the safety
diff), the production audit that a raise needs, asymmetric accuracies or a dependence
term if the checks call for them, `VOLATILITY`, V1, V4, and bot and human V3.
