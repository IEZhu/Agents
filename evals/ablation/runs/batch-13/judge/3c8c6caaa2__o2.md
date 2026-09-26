# Case incident-thread-postmortem-facts-en

## Conversation so far
(none)

## Latest user message
I'm writing the postmortem for yesterday's checkout incident and I wasn't in the channel. Here's the #inc-checkout thread export. Can you give me: the actual root cause, the key timeline (when it started, when it was mitigated), why the first rollback didn't help, and the follow-ups with owners and ticket numbers?

14:02 PagerDuty: [TRIGGERED] checkout-api p99 latency > 2s
14:04 Priya: looking. DB CPU at 85%, might be the new index migration
14:09 Tom: migration finished 13:40 and CPU was fine at 13:50. not convinced
14:11 Priya: killing the analytics query on the replica just in case
14:15 Priya: no change, p99 still 2.4s
14:18 Tom: errors are all from eu-west pods only. us-east is clean
14:20 Ana: we shipped checkout-api v3.18.0 at 13:55 to eu-west only (canary)
14:22 Tom: rolling back eu-west to v3.17.2
14:31 Tom: rollback done, p99 still 2.1s. so not the code??
14:34 Ana: v3.18.0 rollout also pushed the new configmap. HTTP client pool size to payments-gw went from 200 to 20 - typo, it was supposed to stay 200. image rollback doesn't revert the configmap, that's managed separately in the config repo
14:40 Ana: reverted configmap, restarting pods
14:47 Priya: p99 back to 180ms, errors cleared
14:50 Tom: ok so DB was a red herring. Priya can you still own the analytics query on the replica? it's heavy regardless
14:52 Priya: yep, DATA-812
14:55 Ana: I'll add a validation check for pool sizes in config CI
15:03 Tom: actually Ana is out from Monday. Marco can you take the config CI check instead?
15:05 Marco: sure, OPS-441
15:06 Tom: and I'll write up why the image rollback didn't cover config - should config be versioned with the image? let's discuss at Thursday's review
15:10 Priya: side note, ignore my earlier DB number, the 85% CPU was the replica not the primary
15:12 Ana: also for the timeline - 13:55 was when the deploy pipeline started, the eu-west canary actually went live at 13:58

## Rubric (what a strong answer does)
1. Identifies the root cause as the configmap change shipped with the v3.18.0 eu-west rollout that cut the payments-gw HTTP client pool size from 200 to 20, and says the DB was not the cause.
2. Explains that rolling back the image to v3.17.2 did not help because the configmap is managed separately and was not reverted by the image rollback; mitigation came from reverting the configmap and restarting pods, confirmed at 14:47.
3. Gives a timeline consistent with the thread, using Ana's late correction: v3.18.0 canary live in eu-west at 13:58 (not 13:55), alert at 14:02, impact limited to eu-west, recovery confirmed at 14:47.
4. Lists follow-ups with the final owners: DATA-812 (analytics query on the replica) with Priya, OPS-441 (pool size validation in config CI) with Marco rather than Ana, and Tom's write-up and discussion of versioning config with the image at Thursday's review.

## Answer A
## Root cause

The checkout-api v3.18.0 rollout also shipped a new configmap. That configmap had a typo that cut the HTTP client pool size to payments-gw from **200 to 20**. The intended value was to stay at 200. Only eu-west was affected, because v3.18.0 went to eu-west only as a canary. us-east stayed clean.

The thread doesn't explain how the smaller pool caused the latency. The likely mechanism is requests queuing for a free connection to payments-gw, but that is my inference, so confirm it with Ana before you put it in the postmortem.

**The DB was a red herring.** Two reasons:
- The index migration finished at 13:40, and CPU was fine at 13:50.
- Priya later corrected her 85% CPU figure: it came from the **replica**, not the primary. Killing the analytics query on the replica (14:11) changed nothing.

## Key timeline

| Time | Event |
|---|---|
| 13:40 | DB index migration finishes (not related) |
| 13:55 | v3.18.0 deploy pipeline **starts** (eu-west canary) |
| **13:58** | **eu-west canary goes live: start of impact** |
| 14:02 | PagerDuty alert: checkout-api p99 > 2s |
| 14:04–14:15 | Investigation follows the DB theory; killing the replica analytics query has no effect |
| 14:18 | Errors isolated to eu-west pods |
| 14:22 | Image rollback to v3.17.2 starts in eu-west |
| 14:31 | Rollback done; p99 still 2.1s |
| 14:34 | Pool-size typo in the configmap identified |
| **14:40** | **Configmap reverted and pods restarted: mitigation applied** |
| **14:47** | **p99 back to 180ms, errors cleared: recovery confirmed** |

Two timeline details to watch:
- **Use 13:58 as the start of impact, not 13:55.** Ana's correction at 15:12 says 13:55 was only when the pipeline started. The alert fired 4 minutes after the canary went live.
- **Decide whether "mitigated" means 14:40 or 14:47.** 14:40 is when the fix was applied; 14:47 is when recovery was confirmed. State which one you use. Impact lasted about 49 minutes (13:58–14:47), and detection to recovery took about 45 minutes (14:02–14:47).

## Why the first rollback didn't help

The 14:22 rollback reverted only the **container image**. The configmap is managed separately in the config repo, and an image rollback doesn't touch it. After the rollback, v3.17.2 pods were still running with a pool size of 20, so p99 stayed at 2.1s. Only reverting the configmap and restarting the pods (14:40) fixed it.

Our rollback procedure assumes that "rolling back the release" reverts everything the release changed. That assumption is wrong, and it's worth calling out in the postmortem.

## Follow-ups

| Owner | Action | Ticket |
|---|---|---|
| Priya | Fix or optimize the heavy analytics query on the replica (not the cause, but "heavy regardless") | DATA-812 |
| Marco | Add a pool-size validation check to config CI | OPS-441 |
| Tom | Write up why the image rollback didn't cover config, including whether config should be versioned with the image. Discuss at Thursday's review. | **None given** |

Notes:
- **Ana is not the owner of the config CI check.** She volunteered at 14:55, but Tom reassigned it to Marco at 15:03 because Ana is out from Monday.
- **Tom's item has no ticket number.** Ask him for one, or create one, before you publish.
- No follow-up in the thread covers **how the typo passed review**, or **why the canary didn't catch the regression or roll back automatically** during the 4 minutes before the alert. Consider adding both as open questions in the postmortem.


## Answer B
## 1. Root cause

A config typo shipped with the checkout-api v3.18.0 canary in eu-west. The new configmap set the **HTTP client pool size to payments-gw to 20 instead of 200**. The value was supposed to stay at 200 (Ana, 14:34).

The thread never states how the small pool caused the slowdown. The likely mechanism is that requests queued waiting for a connection to payments-gw, which produced the p99 latency and the errors. The fix confirms the config caused it: reverting the configmap and restarting pods cleared the problem within minutes.

The database was not the cause (Tom, 14:50). Two things confirm this:
- The 85% CPU figure was on the **replica, not the primary**. Priya corrected this at 15:10, so don't use the 14:04 number in the writeup.
- Killing the analytics query on the replica changed nothing (14:15).

## 2. Key timeline

| Time | Event |
|---|---|
| 13:40 | Index migration finishes. CPU is normal at 13:50, so it's unrelated |
| 13:55 | Deploy pipeline starts for v3.18.0 (eu-west canary) |
| **13:58** | **Canary goes live in eu-west. This is the effective start of impact** (Ana's correction, 15:12) |
| 14:02 | PagerDuty alert: checkout-api p99 > 2s. This is detection |
| 14:04–14:15 | Investigation follows the DB lead. Analytics query on the replica is killed, with no effect |
| 14:18 | Errors isolated to eu-west pods. us-east is clean |
| 14:22–14:31 | Image rolled back to v3.17.2. p99 is still 2.1s |
| 14:34 | Pool-size typo in the configmap identified |
| **14:40** | **Configmap reverted and pods restarted. This is the mitigation action** |
| **14:47** | **p99 back to 180ms and errors cleared. This is confirmed recovery** |

Approximate durations:
- Time to detect: about 4 min (13:58 to 14:02)
- Time to mitigate: about 42 min from start to the revert, or about 49 min from start to confirmed recovery

Two things to be careful about:
- **Use 13:58 as the start, not 13:55.** 13:55 is when the pipeline started, not when the canary went live.
- **The thread doesn't give a date.** Add yesterday's date yourself.

## 3. Why the first rollback didn't help

The rollback at 14:22 reverted only the **container image** (v3.18.0 back to v3.17.2). The bad value was in the **configmap**, which the v3.18.0 rollout had pushed at the same time. The configmap is managed separately in the config repo, and an image rollback doesn't revert it (Ana, 14:34).

The old code was therefore still running with the new pool size of 20, so p99 stayed at 2.1s. The underlying gap is that **the image and its config are versioned and rolled back separately**, even though they ship together.

## 4. Follow-ups

| Owner | Action | Ticket |
|---|---|---|
| Marco | Add a validation check for pool sizes in config CI. This was reassigned from Ana, who is out from Monday | OPS-441 |
| Tom | Write up why the image rollback didn't cover config, and put "should config be versioned with the image?" on the agenda for Thursday's review | — (none given) |
| Priya | Fix the heavy analytics query on the replica. This is not an incident cause: Tom asked her to own it "regardless" | DATA-812 |

## Gaps to fill before publishing

- **Ownership of OPS-441.** Ana offered first, then it moved to Marco. Make sure the postmortem lists Marco as the owner.
- **Tom's item has no ticket.** It's also the fix that addresses the rollback gap directly, so it probably deserves one.
- **"Thursday's review" has no date.** Put the actual date in the postmortem.
- **Customer impact isn't in the thread.** There are no error counts, failed-checkout numbers or affected-user figures. Get these from metrics.
- **Whether the configmap change reached only eu-west isn't stated.** us-east being clean suggests it did, but confirm this.
- **How the typo got through review isn't discussed.** The CI check (OPS-441) catches it after the fact. The postmortem should also ask why a 200 → 20 change passed the config repo review.

