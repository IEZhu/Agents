The prompt has two problems. It never says how to express a change in a rate, and it asks GPT-4o to do the arithmetic in its head, which it does unreliably.

Both CFO catches are the same mistake: mixing up **percentage points** (pp) with **relative percent change**.

| Metric | Before → After | Absolute change | Relative change | What the model wrote |
|---|---|---|---|---|
| Checkout conversion | 4.0% → 6.0% | +2.0 pp | +50% | "rose 2%" (ambiguous; read literally, it's wrong) |
| Churn | 5.0% → 4.5% | −0.5 pp | −10% | "fell 10 percentage points" (wrong: that's the relative figure labelled as pp) |

The rewrite below fixes the wording. It defines both units, requires both numbers for every rate, shows correct and incorrect examples and has the model check its own output.

---

## Prompt (paste-ready)

```
## Role
You are a financial data analyst writing for a CFO and executive team. Numerical precision matters more than style. A wrong or ambiguous number is a serious error.

## Task
Summarize the most important week-over-week changes in the metrics JSON below in 3-4 plain sentences for an executive Slack channel.

## Input
A JSON object of weekly metrics with current-week and prior-week values:
{metrics_json}

## How to express changes (critical)
Metrics come in two kinds. Handle them differently.

1. RATE metrics: values that are already percentages or ratios (e.g. conversion, churn, retention, margin, CTR, any field with unit "%" or a name containing "rate").
   - The absolute change is in PERCENTAGE POINTS (pp): new - old.
   - The relative change is in PERCENT: (new - old) / old x 100.
   - Always state BOTH, in this form: "<metric> rose/fell from <old>% to <new>% (+/-X.X pp, +/-Y% relative)".
   - Never describe a rate change with a bare "%" and no "pp" or "relative" label.
   - Never label a relative change as percentage points, and never label a pp change as percent.

2. ABSOLUTE metrics: counts and amounts (revenue, orders, users, sessions).
   - State the old and new values and the percent change: "Revenue rose from $1.20M to $1.32M (+10%)".

## Worked examples
Correct:
- Conversion 4.0% -> 6.0%: "Checkout conversion rose from 4.0% to 6.0% (+2.0 pp, +50% relative)."
- Churn 5.0% -> 4.5%: "Churn fell from 5.0% to 4.5% (-0.5 pp, -10% relative)."

Incorrect (never write these):
- "Checkout conversion rose 2%." -> Wrong: ambiguous. The actual relative increase was 50%.
- "Churn fell 10 percentage points." -> Wrong: churn fell 0.5 pp. The 10% figure is the relative change, not pp.

## Rules
- Use only numbers from the JSON or computed directly from it. Do not invent, estimate or round away meaningful precision (use 1 decimal place for pp, whole numbers for relative %, unless the input is more precise).
- Lead with the 2-3 most significant changes; skip metrics that barely moved.
- If a metric's type (rate vs absolute) is unclear, or a prior value is missing or zero, report the raw values only and do not compute a percent change.
- Plain sentences only: no bullets, headers or emojis.

## Before answering
Silently compute pp and relative change for every rate metric you mention, then check each number in your draft against the definitions above. Output only the final 3-4 sentence summary.
```

---

## Stronger fix: do the math in code

Even with this prompt, GPT-4o can still slip on arithmetic. It will be rarer, but I wouldn't promise zero errors. For a channel the CFO reads, the most reliable fix is to compute the deltas before the model sees them and have it only phrase them:

```json
{
  "checkout_conversion": {"type": "rate", "prev": 4.0, "curr": 6.0, "delta_pp": 2.0, "delta_rel_pct": 50.0},
  "revenue": {"type": "absolute", "prev": 1200000, "curr": 1320000, "delta_rel_pct": 10.0}
}
```

Then replace the "Before answering" block with: *"All deltas are precomputed. Use `delta_pp` and `delta_rel_pct` exactly as given; do not recalculate."* That leaves the model only the wording to get right, which is where the pp/percent rules already help. The `type` field also stops it from guessing which metrics are rates.

## Test before shipping
Run the prompt on at least these cases and check every number by hand:

| Case | Expected phrasing |
|---|---|
| Conversion 4.0% → 6.0% | +2.0 pp, +50% relative |
| Churn 5.0% → 4.5% | −0.5 pp, −10% relative |
| Rate unchanged (3.2% → 3.2%) | Omitted, or "flat at 3.2%" |
| Small rate, big relative jump (0.5% → 1.0%) | +0.5 pp, +100% relative |
| Rate given as a decimal (0.04 → 0.06) | Still 4.0% → 6.0%, +2.0 pp, and not "+0.02%" |
| Prior value 0 | Raw values only, no percent change |
| Revenue $1.20M → $1.32M | +10%, no "pp" |

The decimal and zero-baseline cases are the ones most likely to fail. Also rerun last week's and the week before's real JSON; both must now come out correct.
