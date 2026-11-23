# Validated claims

A per-workspace store of verdicts about factual claims, and a filter that turns a
claim's verdicts into the probability that it holds now: agreeing checks raise it,
time relaxes it, agreeing checks that fail together cannot inflate it, and a
contradiction is flagged instead of silently flipping the claim. It is layer 7 of the
[delegation reliability plan](delegation-reliability-plan.md#layer-7--importance-and-a-protected-store-for-validated-facts),
built on the verification levels the [thread-close flow](../flows/thread-close.md)
already assigns. A Kalman filter from the same work reports two continuous drifting
quantities elsewhere: the recent acceptance rate of delegated workers and the typical
warmup time.

## Tools

| Tool | What it does |
|---|---|
| `claim_record` | Append one verdict: `subject`, `claim`, `verdict` (`confirmed` or `refuted`), `level` (`V1`..`V4`; `V0` is refused), and optionally `importance`, `volatility`, `evidence` and the `contract_revision` and `result_sha256` of the validated worker result it came from. Returns the claim's new state, its state before, the verdict's predictive probability and `contradiction`. A `workspace` argument outside the registered root is refused with `workspace_mismatch`, and nothing is written |
| `claim_lookup` | Read one claim with its newest verdicts, by `key` or by `subject` with `claim`, or list claims narrowed by `query` text and `state`. A `state` listing is sorted by due date, so `state="stale"` reads as a work queue |

Record a verdict after a check has established it, and look a claim up before
checking it again. A `supported` claim whose `usable_for` includes the importance
you need can be reused as it stands; anything else needs a fresh check. The store is
for the parent session: a worker receives a stored fact only as a pinned
[artifact](shared-artifacts.md), as data, never as an instruction.

## Levels, families, importance and volatility

The level is how the verdict was established, as in thread-close. Levels belong to
families of routes that fail together: V1 and V3 are both a language model's
judgement, so they share a family.

| Level | Meaning | Family | Family accuracy `c` | Route accuracy `b` | One verdict alone |
|---|---|---|---|---|---|
| `V1` | A source was opened and supports it | model | 0.97 | 0.85 | 0.829 |
| `V2` | A command, test or call produced the result | execution | 0.98 | 0.95 | 0.932 |
| `V3` | A separate fresh-context reviewer checked the current state | model | 0.97 | 0.97 | 0.942 |
| `V4` | The owner accepted it and it held | owner | 0.99 | 0.99 | 0.980 |

Importance is what the claim is used for (plan layer 7). Reuse needs a confirming
level since the last refutation and a confidence:

| Importance | Level needed | Confidence needed |
|---|---|---|
| `routine` | `V1` | 0.75 |
| `load_bearing` (default) | `V2` | 0.85 |
| `critical` | `V3` | 0.95 |

Volatility is how fast the claim can change: `stable` (a published standard, a
released API; the truth switches at 1/730 per day), `normal` (a constant, a signature
or a setting in an active repository; 1/120 per day, the default) and `volatile`
(counts, states, open pull requests; 1/7 per day).

**Every number in these tables is a prior chosen for this design, not a measured
accuracy of any route.** Step 8 of the plan's
[order of work](delegation-reliability-plan.md#order-of-work) measures per-route
sensitivity and specificity on a known-answer control set, the measurement its layer 6
describes, and [Measuring verifier accuracy](verifier-accuracy-measurement.md) designs
it for `FAMILIES` and `ROUTES` in [model.py](../src/claims/model.py). `VOLATILITY` is
a property of claims, not of verifiers, so that set cannot measure it: it needs
re-checks of stored claims at random ages. Because the store keeps verdicts and
recomputes every state on read, a changed parameter re-reads the whole history, so a
measured value reaches the store only through the separate change that note
describes, which applies it to the verdicts of the configuration it was measured on.

## The model

A claim is true or false now (`θ`), and it changes by a jump. Next to `θ` the filter
tracks one hidden reading `z_f` per family: what that family as a whole would say
about the claim now. A family reads the truth correctly with probability `c_f`; a
route of that family reads its family's reading correctly with probability `b`. The
belief is a distribution over the 16 combinations of `θ` and the three readings,
and the filter is the exact forward recursion of that hidden Markov model:

```
predict over Δ days: the truth switches at λ per day either way; every switch redraws the readings
  none = e^(−λΔ)                      no switch: the combination is kept as it is
  odd  = (1 − e^(−2λΔ)) / 2           an odd number of switches: the other truth, fresh readings
  even = 1 − odd − none               an even number, at least two: the same truth, fresh readings
  w(θ, z) ← w(θ, z)·none + q_θ(z)·(P(θ)·even + P(not θ)·odd)
  where q_θ(z) = Π_f c_f^[z_f = θ] · (1 − c_f)^[z_f ≠ θ]
update on verdict y from a route of family f with accuracy b:
  w(θ, z) ← w(θ, z)·(b if y agrees with z_f else 1 − b), then normalise
confidence = Σ_z w(1, z)
```

The kernel is exact for any split of time, so two predicts of seven days equal one
of fourteen, and after a long quiet spell nothing of an old family reading remains.

Between verdicts the confidence relaxes towards one half,
`p(t) = 1/2 + (p − 1/2)·e^(−2λt)`, in either direction: an old confirmation and an
old refutation both become "not known".

**Correlated verifiers.** Agreeing verdicts of one family cannot push the claim's
log-odds past `logit(c_f)`, however many there are: twelve V3 confirmations reach
0.97, not 0.9999. The plan records strongly coincident failures across agent
harnesses and models (429 coincident failures against 115 expected under
independence), so ten agreeing checks of one kind are not ten independent proofs.
Confidence beyond a family's cap needs a second, independent family: a V2, a V3 and a
V4 together reach 0.9999.

**Due date.** From the relaxation, the time until the confidence falls to the bound
`b` of the claim's importance has a closed form:
`t = ln((p − 1/2) / (b − 1/2)) / (2λ)`. `revalidate_at` is that moment, fixed at the
last verdict. A contested claim, or one whose importance needs a stronger level than
any confirming verdict since the last refutation, is due immediately.

**Contradiction.** Before each verdict the filter knows how likely that verdict was.
A verdict whose predictive probability is below `1 / (1 + 6.63) ≈ 0.131`, the
chi-square 0.99 quantile the plan uses as its gate, is a contradiction, and the claim
is `contested` until a later verdict is not surprising. The update itself is exact,
so a contradiction needs no special arithmetic. A refutation of a freshly confirmed
claim is a contradiction; a refutation of a claim nobody checked for months usually
is not, because its confidence has relaxed enough that a change is no surprise. A
refutation by the same family that confirmed it is mostly blamed on that route's
own error, so the confidence moves little, but the claim is still contested. A
contradiction is a reason to validate again, from another family where possible,
never a ruling on which verdict is right.

**States.** `supported`: confidence at least 0.75. `refuted`: at most 0.25.
`stale`: it was decided either way at the last verdict, and time alone moved it back.
`contested`: the last verdict was a contradiction. `uncertain`: none of these.

**Why not a Kalman filter here.** A Kalman filter is the Gaussian special case of
the same predict-update recursion, and a claim is not Gaussian: it is binary and
changes by jumps. On 3000 claims simulated from that model (independent routes,
accuracy 0.95, a change every 60 days on average) the Brier score was 0.0109 for
this filter, 0.0136 for a Kalman filter on the indicator scale with debiased readings,
and 0.025 to 0.047 for a Kalman filter on log-odds readings with or without a
contradiction gate. The simulation is generated from this filter's own model, so it
shows how much the Gaussian approximation costs under it, not external validity.

## Storage

One JSON line per verdict in `data/claims/claims.jsonl` under the workspace root
(directory mode 0700, file mode 0600). On POSIX a writer locks the store file itself,
through the descriptor it opened from the claims directory, so writers serialize on
the file they append to; Windows locks a sidecar file beside it. Nothing is
rewritten or deleted; a later verdict supersedes earlier ones by being filtered
after them, and the earlier ones stay readable. The claim's key is the first 16
hexadecimal characters of the SHA-256 of its normalised subject and statement
(Unicode NFC, case-folded, whitespace collapsed, a trailing full stop dropped), so
the same statement recorded twice is the same claim. A line holds the subject, the
statement, the verdict, level, importance, volatility, up to ten evidence
references and the optional worker attestation. The store refuses new verdicts past
20 MiB. It never follows a symbolic link: a linked `data`, `data/claims` or
`claims.jsonl` (a cloned repository can carry one) answers `unsafe_store` instead of
writing to or reading from the link's target. On POSIX each component is opened from
its parent's descriptor, so a directory swapped for a link after a check cannot
redirect a read or an append; Windows has no descriptor-relative open and checks each
component by name, which leaves that window. The writer requires the claims directory
and file to belong to the user and removes group and other permissions from them,
also when they already existed. A line it cannot use, such as a hand
edit or a write cut short by a full disk, is skipped rather than failing every read,
and the next verdict starts on a fresh line. Claim text is project content and, like `read_shared_memory`, the two tools
are not traced to Langfuse.

What the store does not give: a hash of the content is integrity, not authenticity.
The store is not signed and is not an authority; provenance is not truth.

## Other uses of the filter

A survey of the codebase looked for repeated noisy observations of a drifting
quantity behind a fixed constant or an all-time count. Two are built on
[src/estimation](../src/estimation/):

- **Worker acceptance rate.** `scripts/subagent_tiers_report.py` adds a `recent`
  column and the JSON summary a `recent_acceptance` object per client, tier and
  model: the validations filtered in time order with a 14-day half-life and a 95 %
  band ([rate.py](../src/estimation/rate.py)), next to the all-time counts. See
  [model tiers](subagent-model-tiers.md).
- **Warmup time.** Every successful retrieval warmup folds its duration into a
  scalar Kalman filter on log-seconds ([kalman.py](../src/estimation/kalman.py)); the
  `warming_up` hint then says how long starts usually take on this installation and
  when to retry ([warmup_estimate.py](../src/engine/warmup_estimate.py)).

Both only report. Tier floors, escalation, `WARMUP_WAIT_SECONDS` and every other
bound stay fixed: an adaptive estimate must never loosen a safety bound.

For a success rate the update's mean is the Kalman update with the expected
Bernoulli variance as reading noise, which makes it exactly the conjugate Beta
update, and the variance is the Beta posterior's. A plain linear Kalman filter on
the probability scale was tried first and collapsed to exactly 1 after a long run of
successes, after which it could not learn; drift is therefore fading memory with a
half-life rather than added variance.

Candidates found and not built, in the survey's order of value: the per-rule miss
rate across thread-close records (`findings` already carry slug, status and level),
bot-review latency to replace fixed waits (not recorded today), autodev stage
durations against their fixed timeouts, local inference time per token against job
deadlines, and the accuracy of each family of verifiers, which needs unbiased
labels (a known-answer control set or random audits) before a filter can track it.
Places rejected because an adaptive value would loosen a safety bound or change a
contract: the cooperation heartbeat deadline and checkpoint window, claim and lock
lifetimes, result-size limits, the routing similarity threshold and contract
retention.

## Measuring whether it works

The plan's cut rule applies: if the share of `claim_lookup` reads that find a claim
still inside its due date is negligible on a real workload, or stored claims are
more often stale than fresh when read, the store is a liability and the layer
reduces to the importance rating. `claim_lookup` with `state` gives both numbers for
a workspace.
