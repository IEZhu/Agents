# Validated claims

A per-workspace store of verdicts about factual claims, and a Kalman filter that
turns a claim's verdicts into a confidence that narrows with agreeing checks, widens
while nobody checks, and flags a contradiction instead of silently flipping. It is
layer 7 of the [delegation reliability plan](delegation-reliability-plan.md#layer-7--importance-and-a-protected-store-for-validated-facts),
built on the verification levels the [thread-close flow](../flows/thread-close.md)
already assigns. The same filter also reports two drifting quantities elsewhere:
the recent acceptance rate of delegated workers and the typical warmup time.

## Tools

| Tool | What it does |
|---|---|
| `claim_record` | Append one verdict: `subject`, `claim`, `verdict` (`confirmed` or `refuted`), `level` (`V1`..`V4`; `V0` is refused), and optionally `importance`, `volatility`, `evidence` and the `contract_revision` and `result_sha256` of the validated worker result it came from. Returns the claim's new state, its state before, and `contradiction` |
| `claim_lookup` | Read one claim with its newest verdicts, by `key` or by `subject` with `claim`, or list claims narrowed by `query` text and `state`. A `state` listing is sorted by due date, so `state="stale"` reads as a work queue |

Record a verdict after a check has established it, and look a claim up before
checking it again. A `supported` claim whose `usable_for` includes the importance
you need can be reused as it stands; anything else needs a fresh check. The store is
for the parent session: a worker receives a stored fact only as a pinned
[artifact](shared-artifacts.md), as data, never as an instruction.

## Levels, importance and volatility

The level is how the verdict was established, as in thread-close:

| Level | Meaning | Reads as |
|---|---|---|
| `V1` | A source was opened and supports it | 80 % |
| `V2` | A command, test or call produced the result | 90 % |
| `V3` | A separate fresh-context reviewer checked the current state | 95 % |
| `V4` | The owner accepted it and it held | 98 % |

Importance is what the claim is used for (plan layer 7). Reuse needs both a
confirming level since the last refutation and a lower confidence bound:

| Importance | Level needed | Lower bound needed |
|---|---|---|
| `routine` | `V1` | 0.60 |
| `load_bearing` (default) | `V2` | 0.80 |
| `critical` | `V3` | 0.90 |

Volatility is how fast the claim can go stale: `stable` (a published standard, a
released API: months), `normal` (code and configuration in an active repository:
weeks; the default) and `volatile` (counts, states, open pull requests: days).

**Every number in these tables is a prior chosen for this design, not a measured
accuracy of any route.** The plan's layer 8 measures per-route sensitivity and
specificity on a known-answer control set; once those numbers exist they replace
the `READINGS` table in [model.py](../src/claims/model.py). Because the store keeps
verdicts and recomputes every state on read, changing a parameter re-reads the
whole history under the new calibration instead of mixing two.

## The model

The state is the log-odds that the claim holds now, with a Gaussian belief
`N(m, P)` that starts at even odds with variance 9. A verdict at level `L` is a
reading `z = ±logit(accuracy_L)` with variance `R_L`; between verdicts the state
drifts as a random walk whose variance grows by the claim's volatility per day. The
filter is the exact scalar Kalman filter of
[kalman.py](../src/estimation/kalman.py):

```
predict:  P ← P + q·Δt
update:   K = P / (P + R),  m ← m + K·(z − m),  P ← (1 − K)·P
```

Treating verdicts as readings of one quantity, rather than as independent evidence
to be multiplied, is deliberate. The plan records strongly coincident failures
across agent harnesses and models (429 coincident failures against 115 expected
under independence), so ten agreeing checks of one kind are not ten independent
proofs. Under this model the confidence converges to what the strongest level used
supports and does not compound past it; more checks narrow the band instead, which
lets the claim stay reusable for longer. An exact Bayesian update with per-route
likelihood ratios would compound and is the right model only once independence
between routes is measured, not assumed.

**Confidence and band.** `confidence` is `logistic(m)`; `band` is
`logistic(m ∓ √P)`. Reuse compares the band's lower edge with the importance's bound.

**Due date.** Without a verdict the mean stays and the variance grows linearly, so
the time until the lower edge reaches the bound `b` has a closed form:
`t = ((m − logit(b))² − P) / q`. `revalidate_at` is that moment, fixed at the last
verdict. A contested claim, or one whose importance needs a stronger level than any
confirming verdict since the last refutation, is due immediately.

**Contradiction.** Every verdict's normalised innovation squared,
`NIS = (z − m)² / (P + R)`, is compared with the 0.99 quantile of chi-square with one
degree of freedom (6.63). Above it the verdict is inconsistent with what the store
believed: the prior is widened until the verdict is consistent (`P = (z − m)² − R`),
the verdict is applied, and the claim is `contested` until a later verdict agrees
with where it left the belief. A refutation of a freshly confirmed claim is a
contradiction; a refutation of a claim nobody checked for months usually is not,
because its band has widened enough that a change is no surprise. A contradiction
is a reason to validate again, never a ruling on which verdict is right.

**States.** `supported`: the lower edge clears the routine bound. `stale`: it did
right after the last verdict, and time alone pushed it below. `contested`: the last
verdict tripped the gate. `refuted`: the upper edge is below 0.40. `uncertain`:
none of these.

## Storage

One JSON line per verdict in `data/claims/claims.jsonl` under the workspace root
(directory mode 0700, file mode 0600), appended under a sidecar lock. Nothing is
rewritten or deleted; a later verdict supersedes earlier ones by being filtered
after them, and the earlier ones stay readable. The claim's key is the first 16
hexadecimal characters of the SHA-256 of its normalised subject and statement
(Unicode NFC, case-folded, whitespace collapsed, a trailing full stop dropped), so
the same statement recorded twice is the same claim. A line holds the subject, the
statement, the verdict, level, importance, volatility, up to ten evidence
references and the optional worker attestation. The store refuses new verdicts past
20 MiB. Claim text is project content and, like `read_shared_memory`, the two tools
are not traced to Langfuse.

What the store does not give: a hash of the content is integrity, not authenticity.
The store is not signed and is not an authority; provenance is not truth.

## Other uses of the filter

A survey of the codebase looked for repeated noisy observations of a drifting
quantity behind a fixed constant or an all-time count. Two are built:

- **Worker acceptance rate.** `scripts/subagent_tiers_report.py` adds a `recent`
  column and the JSON summary a `recent_acceptance` object per client, tier and
  model: the validations filtered in time order with a 14-day half-life and a 95 %
  band ([rate.py](../src/estimation/rate.py)), next to the all-time counts. See
  [model tiers](subagent-model-tiers.md).
- **Warmup time.** Every successful retrieval warmup folds its duration into a
  filter on log-seconds; the `warming_up` hint then says how long starts usually
  take on this installation and when to retry
  ([warmup_estimate.py](../src/engine/warmup_estimate.py)).

Both only report. Tier floors, escalation, `WARMUP_WAIT_SECONDS` and every other
bound stay fixed: an adaptive estimate must never loosen a safety bound.

For a success rate the filter's mean update uses the expected Bernoulli variance as
reading noise, which makes it exactly the conjugate Beta update, and keeps the Beta
posterior's variance. A plain linear Kalman filter on the probability scale was
tried first and collapsed to exactly 1 after a long run of successes, after which it
could not learn; drift is therefore fading memory with a half-life rather than added
variance.

Candidates found and not built, in the survey's order of value: the per-rule miss
rate across thread-close records (`findings` already carry slug, status and level),
bot-review latency to replace fixed waits (not recorded today), autodev stage
durations against their fixed timeouts, and local inference time per token against
job deadlines. Places rejected because an adaptive value would loosen a safety
bound or change a contract: the cooperation heartbeat deadline and checkpoint
window, claim and lock lifetimes, result-size limits, the routing similarity
threshold and contract retention.

## Measuring whether it works

The plan's cut rule applies: if the share of `claim_lookup` reads that find a claim
still inside its due date is negligible on a real workload, or stored claims are
more often stale than fresh when read, the store is a liability and the layer
reduces to the importance rating. `claim_lookup` with `state` gives both numbers for
a workspace.
