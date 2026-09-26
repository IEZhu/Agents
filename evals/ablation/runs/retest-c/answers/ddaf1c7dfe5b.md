**No, I can't confirm that. Only about 20% of flagged transactions will be fraud. Roughly 4 in 5 flags will be legitimate customers.** The premise that "basically everything flagged is fraud" doesn't hold at a 0.5% fraud rate.

### The math (per 100,000 transactions)

| | Count | Flagged |
|---|---|---|
| Fraud (0.5%) | 500 | 99% caught → **495** |
| Legit (99.5%) | 99,500 | 2% false positive → **1,990** |
| **Total flagged** | | **2,485** |

**Precision (PPV) = 495 / 2,485 ≈ 19.9%**

Fraud is rare, so a 2% false-positive rate applied to the very large legit pool produces about 4x as many false flags as true ones. This is the base-rate fallacy.

### What the note should correct

- **"99% accuracy" is the wrong label.** 99% is **recall (sensitivity)**: the share of fraud that gets caught. It says nothing about how reliable a flag is. The number that matters for auto-blocking is **precision**, and it's about 20%.
- **Customer impact of auto-blocking:** about 1,990 legit transactions blocked per 100k, which is about 2% of all good-customer transactions. At scale that means complaints, churn, and support load. Compare that with 495 frauds stopped.
- **Blocked-to-fraud ratio:** about 4.0 legit blocks for every fraud stopped.

### Caveats

- These numbers assume the 99% and 2% hold in production at the stated 0.5% prevalence. If they came from a test set that was rebalanced or enriched with fraud, real-world performance could differ. That's worth confirming with whoever validated the model.
- Precision is very sensitive to prevalence. If fraud is really 0.25%, precision drops to about 11%. If it's 1%, precision rises to about 33%.
- The 2% FPR is an average. Precision will likely be much higher in the top score band, so a tiered approach could work (see below).

### Recommendation: no-go on blanket auto-block

1. **Keep manual review for flagged transactions.** Pitch the model as a way to cut the review queue to about 2.5% of volume, not as a replacement for review.
2. **Auto-block only a high-confidence tier.** Pick a score threshold where precision is, say, ≥90% on production-like data, and send the rest to review.
3. **Put a dollar figure on the tradeoff.** Compare (fraud losses avoided) with (false blocks × cost per false block: lost revenue, churn, support). Use that to choose thresholds.
4. **Run a shadow or holdout period** before any auto-blocking, and measure real precision on live traffic.

One-liner for the note: *"At 0.5% fraud prevalence, 99% recall and a 2% false-positive rate mean only ~20% of flagged transactions are fraud. Blanket auto-blocking would block ~4 legitimate customers for every fraud stopped."*
