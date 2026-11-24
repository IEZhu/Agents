# Verifier accuracy: stage 1 of the full set

**Status:** run on 2026-11-24 as the first stage of the 240-item measurement, which
the owner approved in stages after [the pilot](verifier-accuracy-pilot.md); the
rest of the set waits for the owner. A stage decides only whether the run goes on:
its bands carry no replacement-rule case and nothing is applied to the claim
filter. The code is [evals/verifier_accuracy](../evals/verifier_accuracy/__init__.py),
driven by the [verifier accuracy flow](../flows/verifier-accuracy.md). Every number
below is an aggregate; no statement or label of the set appears here.

## Why a stage

The pilot's finding 8 showed that the planned design reaches the replacement
rule's ±0.03 for `b_V3` only, so the full run could refute a prior but not confirm
or lower the other three. The owner chose to run the full set in stages: about 220
runs first, then a decision from the measured accuracy, verdict yield, cost and a
projection of the whole set. A stage is part of the full run, not a replacement for
it: its records stay in the set, and the next stage continues from them.

## What changed in the harness

- **Schedule by cluster blocks.** Clusters come in a seeded order, and every slot of
  a cluster's items stays together. `run --stage-runs <n>` takes the leading
  complete clusters whose slots fit `n`. So a stage is a cluster sample of the set,
  and its design effect estimates the whole set's: a stage of scattered items would
  break the clusters up and report `D` near 1. Within what one run takes, slots are
  still shuffled so arms and items interleave.
- **Planning look.** `analyze --look planning` reports the final look's bands with no
  rule case. It also projects the whole set: `(c, b)` drawn from the stage's
  posterior, the full design simulated on the set's clusters with each verdict lost
  at the stage's measured yield, and the fit run under the larger of the calibrated
  floor and the stage's own `D`. It reports the projected half-widths, the share of
  simulated sets that reach ±0.03, the share in each rule case, and the slots left
  with their cost at the stage's mean.

## What ran

- **Set** `control@19d5c8346aaf7b9d`, built at commit `7a127be9` of `dev`, the
  pilot's commit:
  - 240 items, 120 true and 120 false, each from a different file, in 91 clusters
    (52 of them single items, the largest 10 items);
  - the V2 subset has 205 items (104 true, 101 false), so the set has 890 slots;
  - strata: `path` 69, `cli_flag`, `doc_quote`, `name_signature` and
    `numeric_constant` 35 each, `config_default` 20, `pinned_version` 11;
  - false items: 36 near-miss names, 35 paths one segment off, 19 negated
    sentences, 11 off-by-one numbers, 10 wrong units and 9 wrong defaults;
  - no label needed the owner; the builder skipped 158 candidates whose text would
    raise the worker's tier, 91 whose package would select other skills, and 1 whose
    deterministic check disagreed with the class it was built for.
- **Coverage simulation**, 1000 sets per scenario on the set's own clusters: it held
  at a floor of 1.5625 under `D` (simulated coverage 0.995 to 0.999 against the
  nominal 0.9875). The pilot's planned design needed 1.95 in clusters of two or
  three; the lower floor here likely comes from the set's many single-item clusters.
- **Stage**: `--stage-runs 220 --seed 0` took 206 slots, 57 items in 24 complete
  clusters (114 V3-arm and 92 V2-arm slots). Its size-weighted mean cluster size is
  5.28, against 4.93 for the whole set.
- **Configuration**, read from the host records: client `claude`, tier `standard`,
  model `claude-sonnet-5-5`, effort `medium`, one `bundle_revision` for both arms. No
  item mixed configurations. The workers saw the user's global instructions, as in
  the pilot.
- **Runs**: 206 attempts and 11 canary probes, 4 workers at a time, in 10.4 minutes.

## Cost

As the Claude Code CLI reported it for each run: the API list-price equivalent, not a
bill.

| Runs | Count | Mean | Median | Maximum | Total |
|---|---|---|---|---|---|
| V3 arm | 114 | $0.171 | $0.169 | $0.187 | $19.45 |
| V2 arm | 92 | $0.170 | $0.169 | $0.178 | $15.63 |
| Canary probes | 12 | $0.042 | $0.022 | $0.153 | $0.51 |
| **All** | 218 | | | | **$35.58** |

The probes include one run before the stage. A run took 8.8 seconds and 3.1 turns on
average. The 684 slots left would cost about $116 at the stage's mean, plus about
$1.50 of probes, and take about 35 minutes at 4 workers.

## Blinding

All 11 probes, one before each batch of 20, tried the canary four ways (the Read
tool, `cat`, `head` and a Python `open`), and every attempt was refused. No
worker transcript named the control directory, so no run was discarded.

## Statuses

| Arm | Verdicts | Invalid | Level assigned |
|---|---|---|---|
| V3 | 113 | 1 (narration before the header) | 111 `V3`, 2 `unsupported` |
| V2 | 92 | 0 | 92 `V2` |

The verdict yield, the share of launched slots that became a usable verdict at the
family's level, is 0.974 for the model family and 1.0 for the execution family.

## What the stage shows

**Every verdict at a level was right**: 111 of 111 at V3 and 92 of 92 at V2, true
and false items alike, in each of the six strata the stage held (it drew no
`pinned_version` item). The 2 `unsupported` verdicts count at no level. The
pilot had 2 wrong in 64, both false positives on true numeric constants; this stage
held 8 numeric-constant verdicts per level.

**The stage cannot measure `D`.** With no error, the estimates do not vary under
resampling, and the analysis falls back to the mean cluster size: 2.375 for the
model family and 2.0 for the execution family. That fallback is the worst case,
every item of a cluster failing together. In practice the evidence then counts
clusters, not items, which is why the true and the false halves of the execution
family get the same band (13 clusters each). The stage was meant to measure `D`
among other things; it cannot do so without errors, and the full run will not
either while it observes none.

Bands of the planning look (final-look quantiles, no rule case):

| Parameter | Prior | Stage median | Band | Half-width |
|---|---|---|---|---|
| `c_model` | 0.97 | 0.973 | [0.816, 1.000] | 0.092 |
| `b_V3` | 0.97 | 0.986 | [0.899, 1.000] | 0.051 |
| `c_execution` | 0.98 | 0.972 | [0.809, 1.000] | 0.095 |
| `b_V2` | 0.95 | 0.985 | [0.897, 1.000] | 0.051 |

The symmetry, independence and per-stratum checks found nothing, as they cannot
without errors.

## Projection of the whole set

Two views, both for all 240 and 205 items under the calibrated floor or the stage's
`D`, whichever is larger.

**Drawn from the stage's posterior** (200 simulated whole sets). The posterior is
wide, so the draws include accuracies well below what the stage saw:

| Parameter | Median half-width (10th to 90th percentile) | Reaches ±0.03 | Rule 2 | Rule 3 | Rule 4 |
|---|---|---|---|---|---|
| `c_model` | 0.047 (0.029 to 0.074) | 13 % | 13 % | 12.5 % | 74.5 % |
| `b_V3` | 0.027 (0.015 to 0.041) | 63.5 % | 63.5 % | 2.5 % | 34 % |
| `c_execution` | 0.048 (0.030 to 0.076) | 12.5 % | 12.5 % | 23 % | 64.5 % |
| `b_V2` | 0.025 (0.015 to 0.036) | 72 % | 72 % | 0 % | 28 % |

Rule 1 (narrow, at or below the prior) occurred in no simulated set.

**If the rest of the set has no error either**, every item with two right verdicts,
the full fit uses the fallback `D`
of 2.64 (model) and 2.44 (execution), and every band is narrow and above its prior:

| Parameter | Prior | Band | Half-width | Case |
|---|---|---|---|---|
| `c_model` | 0.97 | [0.946, 1.000] | 0.027 | rule 2, stays 0.97 after the audit |
| `b_V3` | 0.97 | [0.973, 1.000] | 0.014 | rule 2, about 0.973 after the audit |
| `c_execution` | 0.98 | [0.942, 1.000] | 0.029 | rule 2, stays 0.98 after the audit |
| `b_V2` | 0.95 | [0.970, 1.000] | 0.015 | rule 2, about 0.970 after the audit |

Rule 2 raises a value only after the production audit, which is not built.

## For the owner

Continuing costs about $118 and 35 minutes. What it can buy:

- **If errors stay this rare**, all four bands come out narrow and above the priors.
  The only values that would move are `b_V2`, from 0.95 to about 0.970, and `b_V3`,
  from 0.97 to about 0.973, and only once the audit exists. Nothing would be lowered.
- **Under the posterior projection**, which allows errors the stage did not see, `b`
  still narrows in about two thirds of the projected runs.
  `c` stays wider than ±0.03 in most of them, with a 12 to 23 % chance of refuting a
  `c` prior.
- **What it cannot buy** is a measured `D` without errors. Narrow bands for `c` would
  rest on the worst-case fallback, which is conservative, not wrong.

## Not done here

No value is applied to the claim filter, and the production audit that rule 2 needs
is not built. `VOLATILITY`, V1 and V4 keep their priors. The set stays in the control
directory for the next stage.
