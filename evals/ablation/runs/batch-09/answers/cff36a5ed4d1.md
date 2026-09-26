**Answers:** (1) **69** use exactly one tool, (2) **7** use none, (3) **25** new Jira seats.

**One thing to watch:** the Jira + Notion line is worded differently from the others. It says "17 use Jira and Notion **but not Linear**," so it leaves out the 8 people who use all three. The other two pair counts include them, as you said. So the full Jira ∩ Notion count is 17 + 8 = **25**, not 17.

### The breakdown

| Region | Calculation | Count |
|---|---|---|
| All three | given | 8 |
| Jira + Linear only | 20 − 8 | 12 |
| Jira + Notion only | given (already excludes Linear) | 17 |
| Linear + Notion only | 15 − 8 | 7 |
| Jira only | 70 − 12 − 17 − 8 | 33 |
| Linear only | 45 − 12 − 7 − 8 | 18 |
| Notion only | 50 − 17 − 7 − 8 | 18 |
| **Use at least one** | sum of the above | **113** |
| **None** | 120 − 113 | **7** |

Check with inclusion–exclusion: 70 + 45 + 50 − 20 − 25 − 15 + 8 = 113. The two methods agree.

1. **Exactly one tool:** 33 + 18 + 18 = **69**
2. **None:** **7**
3. **New Jira seats:** everyone on Linear who isn't already on Jira, which is 45 − 20 = **25**. The 20 Jira + Linear users already have Jira seats.

### Why the reading matters

If you take 17 as the full Jira ∩ Notion count, including triple users, you get 70 + 45 + 50 − 20 − 17 − 15 + 8 = **121** users. That's more than your 120 engineers, so that reading can't be right, and anyone in finance who checks the math will catch it. The same misreading would also give "exactly one" as 85 instead of 69. Answer (3) is 25 under either reading.

### Before sending

- **Seats vs. users:** the 25 assumes you currently have exactly 70 paid Jira seats, one per active user. If you already have spare licenses, subtract them. If Jira bills in tiers, round up to the next tier.
- **Survey data:** "actively use" is self-reported. Linear's admin panel will have the actual list of active Linear users, which is a better number to base a purchase on than survey responses.
