# Case ab-test-revenue-projection-en

## Conversation so far
(none)

## Latest user message
We ran a 3-week A/B test on our new checkout page. Control: 12,000 users, 420 purchases (3.50%). Variant B: 12,000 users, 492 purchases (4.10%). Our analyst says it's significant at p < 0.05. Average order value is about $62 in both arms and we get roughly 180k checkout visitors per quarter. I'm presenting to the board on Thursday. Can you confirm B is the winner and tell me how much extra revenue shipping it will bring next quarter? I need a dollar figure for the slide.

## Rubric (what a strong answer does)
1. Confirms that B is likely better than control (the difference is statistically significant at the 5% level) rather than dismissing the result as noise
2. Makes clear that the size of the uplift is much less certain than its direction: gives a range for the extra revenue (roughly $13k-$120k per quarter, or an equivalent wide interval) or explicitly says the point-estimate figure could be far off, instead of presenting a single dollar number as the expected result
3. Any point-estimate arithmetic is correct: about 0.6 percentage points x 180,000 visitors x $62 is roughly $67k per quarter
4. Gives the user a usable way to present it on the slide (e.g. an expected figure with a range, or a conservative lower-bound figure) rather than refusing to give a number

## Answer A
**Short answer:** Yes, B wins on conversion. Your analyst is right that the result is significant. The revenue figure is less certain than the conversion result, though. A defensible slide number is **about $67k of extra revenue next quarter, with a plausible range of roughly $13k to $121k**. Present that range, not only the single number. The $67k is more likely an overestimate than an underestimate.

## 1. Is B the winner?

I reran the numbers with a two-proportion z-test using the figures you gave:

| | Control | Variant B |
|---|---|---|
| Users | 12,000 | 12,000 |
| Purchases | 420 | 492 |
| Conversion | 3.50% | 4.10% |

- **Absolute lift:** +0.60 percentage points (pp)
- **Relative lift:** +17.1%
- **z ≈ 2.43, two-sided p ≈ 0.015.** This matches your analyst's p < 0.05.
- **95% confidence interval (CI) on the lift:** about **+0.12 pp to +1.08 pp**, which is roughly +3% to +31% relative.

The whole interval is above zero, so B very likely converts better. But the data can't tell apart a small gain from a large one, and the interval is wide.

## 2. Revenue for next quarter

Formula: 180,000 visitors × lift × $62 average order value (AOV).

| Scenario | Lift | Extra purchases | Extra revenue |
|---|---|---|---|
| Low end of 95% CI | +0.12 pp | ~210 | **~$13k** |
| Point estimate | +0.60 pp | ~1,080 | **~$67k** |
| High end of 95% CI | +1.08 pp | ~1,950 | **~$121k** |

## 3. Why I wouldn't show $67k alone

1. **Winner's curse.** If the true lift is 0.6 pp, this test had only about 68% power to detect it. Tests with modest power that come out significant tend to overstate the effect. The real lift is more likely below 0.6 pp than above it.
2. **"About $62 in both arms" hasn't been tested.** The revenue math assumes AOV didn't change. A new checkout can shift basket size, for example by removing upsells or changing discounts. Ask your analyst to test **revenue per visitor** directly. That is the metric the board actually cares about.
3. **Users vs. visitors.** The test counted *users*, but your 180k is *checkout visitors*. If one user can make several visits, applying a per-user lift to visitor counts inflates the result. Make sure both are the same unit.
4. **Test hygiene.** Check these with your analyst:
   - Was the 3-week length fixed in advance, or was the test stopped when it hit significance? Peeking inflates false positives.
   - Was conversion the one pre-registered primary metric, or one of several?
   - Did the lift hold steady across the 3 weeks? A novelty effect would show as a lift that shrinks over time.
5. **Next quarter isn't the test period.** Seasonality, traffic mix and promotions all change the baseline. The $62 AOV and 3.5% baseline may not carry over.
6. **Revenue, not profit.** $67k is extra gross revenue. The margin impact is smaller.

## Suggested slide wording

> "New checkout lifted conversion from 3.5% to 4.1% (+17%, p ≈ 0.015). Projected impact: **~$67k additional revenue next quarter** (95% range: $13k–$121k), assuming stable order value and traffic."

If you want a figure that is less likely to be missed, use a conservative number of about **$40–50k**. That is my judgment call to allow for the winner's curse, not a statistical output. Whichever you choose, say which one it is on the slide. Also ask your analyst to confirm the revenue-per-visitor result before Thursday. It's the check most likely to change the dollar figure.


## Answer B
**Short answer:** Yes, B is the likely winner. The result is statistically significant, so your analyst is right. The dollar figure is the weak part. My best estimate is about **$67k extra revenue next quarter**, but the 95% interval runs from about **$13k to $121k**, and the true number is more likely below $67k than above it. Put the range on the slide, not a single number.

### 1. Significance check

I worked these out by hand from your numbers. The script at the end reproduces them.

| Metric | Value |
|---|---|
| Control conversion | 420 / 12,000 = 3.50% |
| Variant B conversion | 492 / 12,000 = 4.10% |
| Absolute lift | +0.60 percentage points |
| Relative lift | +17.1% |
| Two-proportion z-test (pooled) | z ≈ 2.43, two-sided p ≈ 0.015 |
| 95% CI on absolute lift | +0.12 pp to +1.08 pp |
| 95% CI on relative lift | about +3% to +31% |

- **H0:** B converts at the same rate as control. **H1:** the rates differ.
- p ≈ 0.015 is below 0.05, so we reject H0. B's conversion rate is very likely higher.
- The interval is wide, though. The data fit anything from a small +3% lift to a large +31% lift.

### 2. Revenue projection

Formula: extra revenue = quarterly visitors × lift in conversion × AOV.

| Scenario | Lift | Extra purchases/quarter | Extra revenue |
|---|---|---|---|
| Low (95% CI lower bound) | +0.12 pp | ~210 | **~$13k** |
| Point estimate | +0.60 pp | ~1,080 | **~$67k** |
| High (95% CI upper bound) | +1.08 pp | ~1,950 | **~$121k** |

(180,000 × 0.006 × $62 = $66,960.)

### 3. Why $67k is probably too high

1. **The test was underpowered for this effect.** With 12,000 users per arm at a 3.5% baseline, the smallest lift the test could reliably detect (80% power, α = 0.05) is about 0.69 pp. The observed lift of 0.60 pp is below that. When an underpowered test does come out significant, its effect estimate tends to be too high (the "winner's curse"). Expect the real lift to be smaller than 0.6 pp.
2. **Novelty effect.** Three weeks is short. Some of the lift may come from returning users reacting to something new, and that fades.
3. **AOV "about equal" hasn't been tested.** The projection assumes B doesn't change basket size. Ask your analyst to compare **revenue per visitor** directly, with its own test (e.g. Mann-Whitney U or a bootstrap CI). That metric is the one the board actually cares about.
4. **Check the population match.** 180k/quarter is about 41.5k visitors over 3 weeks, but the test had 24k. Confirm the test covered the same kind of checkout visitor as the 180k (e.g. not just one device, region or traffic source).
5. **Process check.** Was the 3-week length and sample size fixed in advance? If someone checked results repeatedly and stopped once it hit p < 0.05, the real p-value is higher than 0.015.
6. **This is revenue, not profit.** Margin, returns and implementation cost aren't included.

### 4. What to put on the slide

> **Variant B lifted checkout conversion from 3.50% to 4.10% (+17%, p ≈ 0.015).**
> **Projected impact: ~$67k additional revenue next quarter (95% range: $13k–$121k).**
> Recommendation: ship B and monitor conversion for the first 4–6 weeks to confirm the lift holds.

If you have to use one number, a conservative figure like "**$30–70k expected**" is easier to defend than a flat $67k. But the range with its source is the honest version, and boards usually respond well to it.

### 5. Verification script

```python
import numpy as np
from scipy import stats
from statsmodels.stats.proportion import proportions_ztest, confint_proportions_2indep

conv = np.array([492, 420])   # B, control
n    = np.array([12000, 12000])

z, p = proportions_ztest(conv, n)
lo, hi = confint_proportions_2indep(conv[0], n[0], conv[1], n[1], method="wald")
diff = conv[0]/n[0] - conv[1]/n[1]

visitors, aov = 180_000, 62
print(f"z={z:.3f}, p={p:.4f}")
print(f"lift={diff*100:.2f} pp, 95% CI [{lo*100:.2f}, {hi*100:.2f}] pp")
print(f"revenue: point=${visitors*diff*aov:,.0f}, "
      f"low=${visitors*lo*aov:,.0f}, high=${visitors*hi*aov:,.0f}")
```

**Bottom line:** Ship B. The evidence that it converts better is solid. For Thursday, present the uplift as roughly $67k with a range of $13k–$121k, and say the lower half of that range is more likely.

