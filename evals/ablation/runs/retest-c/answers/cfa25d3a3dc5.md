**I can't confirm Product's claim. With your numbers, only about 20% of flagged transactions will be fraud. The other ~80% are legitimate customers.** Auto-blocking everything the model flags would block about 4 legitimate transactions for every fraudulent one it stops.

### The math (per 100,000 transactions)

| | Count |
|---|---|
| Fraudulent (0.5%) | 500 |
| Fraud flagged (99% recall) | **495** |
| Legitimate (99.5%) | 99,500 |
| Legit flagged (2% false positive rate) | **1,990** |
| **Total flagged** | **2,485** |

**Precision (PPV) = 495 / 2,485 ≈ 19.9%**

So for every 5 flagged transactions, about 1 is fraud and 4 are legitimate.

### Why "99% accuracy" misleads here

- **99% is recall (sensitivity), not accuracy.** It tells you what share of fraud gets caught. It says nothing about what share of flags are real fraud.
- **Base rate drives the result.** Fraud is rare (1 in 200), so a small 2% false-positive rate on the very large legitimate pool (1,990) is about 4x the number of true catches (495).
- **Accuracy is a misleading metric at this prevalence anyway.** The model's true overall accuracy is about 98.0% ((495 + 97,510) / 100,000). A model that flags nothing scores 99.5% and catches zero fraud.

### Implications for the go/no-go note

1. **As proposed (auto-block all flags): no-go.** About 2% of all legitimate transactions would be blocked. At 100k transactions that is roughly 1,990 good customers a day or week, depending on your volume. Weigh that against 495 frauds caught, in terms of cost per false block (churn, support load, lost revenue) versus cost per missed fraud.
2. **The model is still valuable for triage.** Manual review now sees a queue that is 20% fraud instead of 0.5%, about a 40x enrichment. That is a strong case for keeping review and routing only flags to it.
3. **Consider a tiered policy.** If the model outputs a score, auto-block only the highest-score band, where precision may be much higher, and send the rest to manual review. Get the precision/recall curve at candidate thresholds before deciding. I can't estimate those numbers from the two rates given.

### Caveats to verify before finalizing

- **Where were 99% / 2% measured?** If they came from a balanced or oversampled test set, or a period with a different fraud rate, production performance may differ. They should be validated on a recent, representative holdout.
- **Is 0.5% stable?** Precision is very sensitive to prevalence. At 0.25% fraud, precision drops to about 11%. At 1%, it rises to about 33%.
- **Units matter.** These are per-transaction figures. Customer-level impact depends on how flags cluster across accounts.

**Suggested one-liner for the note:** "At 0.5% fraud prevalence, 99% recall and 2% FPR give ~20% precision: about 4 in 5 auto-blocks would hit legitimate customers. Recommend using the model to prioritize manual review, and assessing threshold-based auto-blocking only for high-score bands after precision is validated on production data."
