# Case managed-postgres-quotes-en

## Conversation so far
(none)

## Latest user message
Need a sanity check before I reply to my CTO. We're moving our Postgres off a self-managed VM to a managed provider. Hard requirements: total under $400/month including everything we need, at least 200 GB storage, point-in-time recovery with at least 7 days retention, one read replica for the analytics team, and data has to stay in the EU (Frankfurt would be nice but legal says any EU region is fine).

Here are my notes from the sales calls, pasted as-is:

Vendor A - $360/mo, 250 GB, PITR 7 days, Frankfurt. Solid support.
Vendor B - $310/mo, 200 GB, Frankfurt, read replica included in the price. PITR mentioned.
Vendor C - $295/mo, 500 GB, PITR 7 days, region is Amsterdam.
Vendor D - $240/mo, 300 GB, PITR 30 days, replicas +$100/mo. Cheapest by far.

Follow-ups from email:
- A: read replicas are billed as a separate instance, +$120/mo. Rep also offered 15% off the whole bill if we sign an annual commit.
- B: on the $310 tier PITR is 3 days; 14 days needs the Pro tier at $450/mo.
- C: read replica is +$90/mo.
- D: only us-east-1 and us-west-2 right now, EU region is "planned for Q1 2027".

My gut says A since it's Frankfurt, and with the annual discount it should land under budget anyway. Am I missing something?

## Rubric (what a strong answer does)
1. Identifies Vendor C as the only option meeting all hard requirements, with a total of $385/month ($295 + $90 replica), and notes that Amsterdam is in the EU and so is acceptable.
2. Explains that A stays over budget once the $120 replica is added ($480/month, and about $408/month even with the 15% annual discount applied to the whole bill), which directly corrects the user's gut choice.
3. Gives the disqualifying reason for B (3-day PITR on the $310 tier, and the $450 tier is over budget) and for D (no EU region yet).
4. Checks every vendor against every requirement, including replica cost in the total, and does not judge on the base price alone.

## Answer A
Yes, you're missing something. **A doesn't fit the budget, even with the discount.** Going by your own notes, **C is the only vendor that meets every hard requirement.**

### The A math

- $360 base + $120 read replica = **$480/mo**
- With 15% off the whole bill: $480 × 0.85 = **$408/mo**, which is still $8 over the $400 cap.
- To get under $400 you'd need about 16.7% off. Even then you'd be tied to an annual contract with almost no room left in the budget.

### All four against the requirements

| Vendor | Monthly total (with replica) | Storage | PITR ≥ 7 days | EU | Result |
|---|---|---|---|---|---|
| A | $480 ($408 with annual commit) | 250 GB | 7 days ✅ | Frankfurt ✅ | ❌ Over budget |
| B | $310 (replica included) | 200 GB | 3 days ❌ (14 days needs Pro at $450, over budget) | Frankfurt ✅ | ❌ Fails on PITR or on budget |
| C | $295 + $90 = **$385** | 500 GB | 7 days ✅ | Amsterdam ✅ | ✅ Meets everything |
| D | $240 + $100 = $340 | 300 GB | 30 days ✅ | US only, EU "planned Q1 2027" ❌ | ❌ Fails data residency |

B and D are out on hard requirements. Don't count on D's roadmap date.

### C still needs checking before you commit

C leaves only $15/mo of headroom. "Total including everything" is where quotes like this usually slip, so get these answers from C in writing:

1. **Replica storage and compute:** is the +$90 replica the same size as the primary and does it include its own storage, or is replica storage billed on top?
2. **Backup/PITR storage:** is the 7-day WAL/backup retention included in $295, or billed per GB? Many providers charge extra for backup storage above some allowance.
3. **Egress and I/O:** are there charges for data transfer or IOPS? Analytics queries on the replica can drive both.
4. **VAT/taxes:** are prices before VAT? As a B2B customer in the EU you probably get reverse charge, but check this with finance.
5. **Region for everything:** the replica, backups and any support or log access should all stay in the EU, not only the primary.
6. **HA/failover:** if you need it, it's probably not in the $385. If it's a requirement, C also goes over budget.

### Draft reply to your CTO

> A is over budget: $480/mo with the required read replica, and $408 even with the 15% annual discount. B's $310 tier only has 3-day PITR, and meeting our 7-day requirement pushes it to $450. D has no EU region yet. C is the only one that meets every requirement: $385/mo with the replica, 500 GB, 7-day PITR, Amsterdam (EU, which legal has approved). I'm confirming in writing that backup storage, replica storage, egress and taxes are included before we commit, since we'd only have $15/mo of headroom.

If Frankfurt really matters, the only way to get it within budget is to negotiate A down to about $400 with the replica, for example with a bigger discount or a cheaper replica tier. You could offer the rep that as a counter.


## Answer B
Yes, you're missing something. **A doesn't fit the budget, even with the discount.** Based on your notes, **C is the only vendor that meets every hard requirement.**

## The math on A

- Base $360 + read replica $120 = **$480/mo**
- 15% annual discount: $480 × 0.85 = **$408/mo**, which is still $8 over the $400 cap
- It also locks you into a 12-month commit to get there

Frankfurt is a nice-to-have. Legal said any EU region is fine, so Frankfurt shouldn't outweigh a hard budget limit.

## Every vendor against the requirements

| | Total w/ replica | Storage | PITR ≥7d | EU | Verdict |
|---|---|---|---|---|---|
| **A** | $480 ($408 annual) | 250 GB ✅ | 7d ✅ | Frankfurt ✅ | ❌ over budget |
| **B** | $310 tier / $450 Pro | 200 GB ✅ | 3d ❌ / 14d on Pro | Frankfurt ✅ | ❌ fails PITR or budget |
| **C** | **$385** | 500 GB ✅ | 7d ✅ | Amsterdam ✅ | ✅ **passes all** |
| **D** | $340 | 300 GB ✅ | 30d ✅ | US only ❌ | ❌ fails residency |

Notes:
- **B:** the cheap tier fails PITR, and Pro fails budget before you even ask whether the replica is still included on Pro.
- **D:** "planned for Q1 2027" is a roadmap promise, not a region you can use. Don't count on it.

## Confirm with C before you commit

$385 leaves only $15 of headroom, so "including everything" matters:

1. **Replica storage.** Is the +$90 all-in, or is replica storage billed separately? If it's billed like A's (a full separate instance), 200+ GB more could push you over.
2. **Backup/PITR storage.** Is WAL and backup storage for 7 days included, or metered separately?
3. **Egress / data transfer.** Analytics pulling from the replica may generate egress charges, depending on where the analytics tools run.
4. **VAT.** Check whether the quotes are net of VAT. For EU B2B it's usually reverse-charged, but confirm how your finance team counts it against the $400.
5. **PITR is exactly 7 days.** That meets the requirement with zero margin. Make sure it's 7 full days and not "up to 7."
6. **Compute sizing.** Make sure the $295 tier's CPU/RAM matches what your current VM actually uses. 500 GB of storage on a small instance won't help if it can't handle the load.

## Suggested line for the CTO

> "C (Amsterdam) is the only option that meets all hard requirements at ~$385/mo with replica. A comes to $408/mo even after a 15% annual discount; B fails either PITR or budget; D has no EU region. Confirming C's quote covers replica storage, backups and egress before we commit."

If C's all-in number comes back over $400, the fallback is to renegotiate A: ask for a bigger discount or a cheaper replica tier. $8/mo is a small gap to close.

