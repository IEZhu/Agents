# Pass gates for rule-outbound-timing, run outbound-timing-01

Written before any answer or verdict existed, so the thresholds are not fitted to
the result. Contexts were built first: 21 pairs, every pair differing by exactly
942 characters, the rule's rendered section, with no other change
(`plan.json`, `build_errors.json` empty).

## Read the per-group rows, not the pooled number

`aggregate.py` reports one row per component, so it pools all 21 cases.
`compare.py` groups by the case `group` field but its sign test still pools every
non-control group into one figure. A rule that always advised waiting would win
`timing-wait` and lose `timing-now` inside that one number. The gates below are
therefore read from the per-group rows of `compare.py`, and the pooled figure is
not a gate.

## Groups

| group | cases | verdicts | what it tests |
|---|---:|---:|---|
| `timing-wait` | 6 | 12 | the delivery moment is a real choice, so the rule should earn its keep |
| `timing-now` | 5 | 10 | sending now is correct, so the rule must not push toward waiting |
| `control` | 10 | 20 | the rule must stay inert; this is the noise floor |

## Gates

1. `timing-wait`: `net` above 0, `robust with` at least 3 of 6, `robust without` 0.
2. `timing-now`: `robust without` 0, and `net` not below 0. Any robust loss here
   means the rule costs the user something when waiting is wrong, which is the
   failure mode the whole design was built to avoid.
3. `control`: `robust without` 0, which is the intrusion regression the request
   rule forbids, and `robust with` at most 1, since a rule that changes answers
   where it should be silent is also a failure even when a judge prefers the change.

A run that passes 1 and fails 2 or 3 is a fail, not a partial pass. Gate 1 alone
is the weakest of the three, because a rule can win the cases it was written for
and still be a net loss everywhere else.

## If the probes do not discriminate

`timing-wait` winning on fewer than 3 of 6 robust cases, with `control` clean,
most likely means the cases do not separate the arms rather than that the rule
is inert. Rewrite the cases; do not relax these gates.

## Known weak cases, declared in advance

The case checker kept four cases it judged weak and said so: `now-water-leak-3am`
(almost every model sends now regardless), `ctl-review-off-by-one` and
`ctl-edge-shorten-after-note` (the arms are likely to answer alike), and
`wait-apology-after-fight` (its timing signal is mild, so a strict reading of the
rule may add no line). Ties on those are expected and are not evidence either way.
