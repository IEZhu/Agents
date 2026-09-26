# Case equipment-rental-deposit-lifecycle-en

## Conversation so far
(none)

## Latest user message
I'm the PM for a camera gear rental marketplace (owners list lenses/bodies, renters book them). Our ops team keeps making mistakes because nobody agrees on what can happen to a rental and when. Current reality: renter books and we put a hold on their card for the deposit (up to $1,500, card holds drop after 7 days so for bookings further out we can only authorize close to pickup), owner has 24h to accept or it auto-declines, pickup is in person, return is in person (rentals run up to 14 days), then owner has 48h to report damage, if they report damage the renter can dispute and our support decides within 5 business days. Last month we had: a deposit released while a damage claim was still open, an owner who marked 'returned' on a rental that was never picked up, and a renter charged a late fee on a booking the owner had cancelled. I need a spec for engineering and ops that defines the rental lifecycle unambiguously so these can't happen. Can you draft it?

## Rubric (what a strong answer does)
1. The spec lists the rental lifecycle as named states with the permitted transitions out of each (who or what triggers them: renter, owner, support, timer), identifies terminal states, and makes clear that any transition not listed is forbidden.
2. It attaches guards and timeouts to the right transitions: auto-decline after 24h without owner acceptance, 'returned' only reachable from 'picked up', deposit release only after the 48h damage window closes with no claim or after a claim/dispute is resolved, and no late fees once a booking is cancelled.
3. It shows explicitly how each of the three incidents from last month is prevented by the defined states and guards.
4. It handles the card-hold constraint beyond the happy path: it notices that a hold placed near pickup can lapse before a long rental ends or before the damage window/dispute is settled, and defines what the system does about it (e.g. re-authorizing, capturing before expiry, or another explicit fallback) and what state the rental moves to if an authorization fails.

## Answer A
Below is a draft you can take to engineering and ops. My main recommendation: stop treating "rental status" as one field. Model the rental as **three linked state machines**: the booking, the deposit and the damage claim. Allow a status change only when a guard passes, and let the deposit move only when the booking or claim changes state, never through a separate button. With that design, all three of last month's incidents are rejected by the system instead of depending on ops to catch them.

Before you circulate it, one thing: **your deposit hold cannot cover your liability window with the current rules.** See section 6. This is a decision for finance, not engineering, and it may be the real cause of incident #1.

---

## Spec: Rental Lifecycle State Model (v1 draft)

### 1. Problem

Ops and the system disagree about which actions are valid for a rental at a given moment. Last month this caused three incidents:

| # | Incident | Root cause (structural) |
|---|---|---|
| 1 | Deposit released while a damage claim was open | Deposit release doesn't check claim status (or the card hold expired on its own, see §6) |
| 2 | Owner marked "returned" on a rental never picked up | "Returned" is reachable without a prior "picked up" |
| 3 | Renter charged a late fee on an owner-cancelled booking | Late fees are computed from dates, not from rental state |

The only baseline is these three incidents. We don't know how many similar errors went unnoticed. Part of the rollout (§11) is an audit of existing bookings to find out.

### 2. Principles (non-negotiable)

1. **One source of truth.** Every status change goes through a single lifecycle service. No direct DB edits and no ops tools that write status fields.
2. **Illegal transitions are rejected**, with an error that says why. They are not logged-and-allowed.
3. **Money follows state.** Deposit and fee actions are side effects of transitions. They cannot be triggered on their own.
4. **Terminal states are terminal.** A booking in `DECLINED`, `CANCELLED`, `NO_SHOW` or `COMPLETED` never accrues charges or changes state again. Corrections are made through a support adjustment (§8), not by reopening the booking.
5. **Every transition is audited**: actor (renter / owner / system / support agent ID), from-state, to-state, timestamp (UTC), reason code.
6. **UIs show only legal actions.** The owner, renter and ops screens render buttons from the current state's allowed transitions, so an invalid button never appears.

### 3. Booking state machine

```
REQUESTED ──accept──▶ CONFIRMED ──handover──▶ ACTIVE ──return──▶ INSPECTION ──window closes──▶ COMPLETED
    │                     │                      │                    │
    ├─decline/timeout─▶ DECLINED                 └─return time+grace─▶ OVERDUE ──return──▶ INSPECTION
    └─renter withdraws─▶ CANCELLED ◀─cancel──────┤                    │
                                                  └─pickup window lapses─▶ NO_SHOW
                                                                     INSPECTION ──damage reported──▶ CLAIM_OPEN ──resolved──▶ COMPLETED
```

| State | Meaning | Terminal |
|---|---|---|
| `REQUESTED` | Renter booked; waiting for owner | No |
| `DECLINED` | Owner declined, or 24h passed (reason recorded) | Yes |
| `CONFIRMED` | Owner accepted; waiting for pickup | No |
| `CANCELLED` | Cancelled before pickup (`cancelled_by`, reason) | Yes |
| `NO_SHOW` | Pickup window passed with no handover | Yes |
| `ACTIVE` | Gear handed over, rental running | No |
| `OVERDUE` | Return deadline + grace passed, gear not returned | No |
| `INSPECTION` | Gear returned; owner's 48h damage window running | No |
| `CLAIM_OPEN` | Owner reported damage; claim machine (§5) running | No |
| `COMPLETED` | All obligations settled; deposit released or captured | Yes |

#### Transition table

Anything not listed here is illegal.

| From | Event | Actor | Guard | To | Side effects |
|---|---|---|---|---|---|
| — | Book | Renter | Valid card; item available for dates | `REQUESTED` | Start T1; deposit → per §4 |
| `REQUESTED` | Accept | Owner | Before T1 expiry | `CONFIRMED` | Block calendar |
| `REQUESTED` | Decline | Owner | — | `DECLINED` | Deposit → `RELEASED` / `NOT_NEEDED` |
| `REQUESTED` | T1 expires | System | — | `DECLINED` (reason: `auto_timeout`) | Same as decline |
| `REQUESTED` | Withdraw | Renter | — | `CANCELLED` | Same as decline |
| `CONFIRMED` | Cancel | Renter or Owner | Before handover | `CANCELLED` | Apply cancellation policy; deposit → `RELEASED` |
| `CONFIRMED` | Deposit auth fails finally (T2) | System | — | `CANCELLED` (reason: `deposit_failed`) | Notify both parties |
| `CONFIRMED` | Handover | Owner + Renter | **Deposit is `SECURED`**; handover code valid (see below) | `ACTIVE` | Record `picked_up_at` |
| `CONFIRMED` | T3 expires | System | — | `NO_SHOW` | Route to support for fault; deposit → `RELEASED` unless a renter no-show fee applies (open Q) |
| `ACTIVE` | Return | Owner + Renter | Return code valid | `INSPECTION` | Record `returned_at`; start T5 |
| `ACTIVE` | T4 expires | System | — | `OVERDUE` | Start late-fee accrual; notify renter |
| `OVERDUE` | Return | Owner + Renter | Return code valid | `INSPECTION` | Stop accrual at `returned_at` |
| `OVERDUE` | Escalation threshold reached (open Q, e.g. 72h) | System | — | stays `OVERDUE` | Create support case (possible loss/theft) |
| `INSPECTION` | Report damage | Owner | Before T5 expiry; photos + description | `CLAIM_OPEN` | Start claim machine (§5) |
| `INSPECTION` | T5 expires | System | No claim filed | `COMPLETED` | Settle late fees (if any); release remainder of deposit |
| `CLAIM_OPEN` | Claim reaches `SETTLED` | System | — | `COMPLETED` | Capture/release per decision |

**Handover and return codes (fixes incident #2).** At pickup the renter's app shows a short one-time code and the owner enters it. At return this reverses: the owner's app shows the code and the renter enters it. One action therefore proves both parties were present. A single party cannot mark a rental picked up or returned. If a device fails, support can perform the transition with evidence, recorded as a support override (§8).

### 4. Deposit state machine

| State | Meaning |
|---|---|
| `SCHEDULED` | Pickup is too far out to authorize; auth planned for T2 |
| `AUTHORIZED` | Hold placed on the card |
| `SECURED` | Deposit is guaranteed to cover the **full liability window** (definition depends on the §6 decision) |
| `RELEASED` | Hold voided / funds returned (terminal) |
| `CAPTURED` | Full or partial capture for damage or fees; remainder released (terminal) |
| `FAILED` | Could not authorize after retries (terminal; triggers booking cancel) |

**Guards that fix incident #1:**
- `→ RELEASED` is allowed only when the booking enters `DECLINED`, `CANCELLED` or `NO_SHOW`, or `COMPLETED` with no claim in a non-`SETTLED` state and no unsettled fees.
- `→ CAPTURED` is allowed only from a settled claim decision or computed late fees at settlement.
- There is no "release deposit" button in ops tools. Ops can only request a support override (§8).
- **Hold-expiry monitor:** if an authorization is due to expire while the booking is in `ACTIVE`, `OVERDUE`, `INSPECTION` or `CLAIM_OPEN`, the system must act (re-auth or capture, per §6) before expiry. If that fails, it pages ops. A hold must never be allowed to lapse silently while liability exists.

### 5. Damage claim state machine

| From | Event | Actor | To |
|---|---|---|---|
| — | Owner reports damage in window | Owner | `AWAITING_RENTER` (start T6) |
| `AWAITING_RENTER` | Renter accepts | Renter | `SETTLED` (capture agreed amount) |
| `AWAITING_RENTER` | Renter disputes | Renter | `UNDER_REVIEW` (start T7) |
| `AWAITING_RENTER` | T6 expires | System | **open Q:** treat as accepted, or send to review |
| `UNDER_REVIEW` | Support decides | Support | `SETTLED` (capture 0–N, reason recorded) |
| `UNDER_REVIEW` | T7 at risk (e.g. 1 business day left) | System | stays; escalates to support lead |

A claim for more than the deposit is captured up to the deposit amount. Recovering the remainder is out of scope for v1 (§10).

### 6. Deposit coverage: decision needed before build

Using the constraints you gave, the time the deposit may need to stay secured is:

| Segment | Max duration |
|---|---|
| Auth before pickup (if done at T-48h) | 2 days |
| Rental | 14 days |
| Return grace + overdue | ≥ 1 day, unbounded if gear is not returned |
| Damage window | 2 days |
| Renter response (proposed) | 2 days |
| Support decision | 5 business days ≈ 7 calendar days |
| **Total** | **~28 days, vs a 7-day hold** |

Even a 3-day rental reaches about 7 days by the end of inspection, so any claim on it outlives the hold. **A single authorization hold cannot secure the deposit for most real claims.** It's possible incident #1 wasn't anyone clicking "release" at all, and the hold simply dropped. That's worth checking in the processor logs.

Options:

| Option | How | Pros | Cons |
|---|---|---|---|
| A. Re-authorize | New auth before expiry, then void the old one | Renter never sees a charge | Re-auth can fail mid-rental with gear out; renter may briefly see a double hold |
| B. Capture at handover | Auth ahead of pickup, capture at handover, refund at settlement | Funds guaranteed; simplest state logic | Renter sees a real charge; refunds take days to appear; processor fees on captured amounts may not be refunded; possible legal/regulatory questions about holding customer funds |
| C. Hybrid | Hold if the booking's worst-case window fits in the auth life, otherwise capture | Best renter experience for short rentals | Two code paths; claims can still extend past the hold, so this needs A or B as a fallback anyway |

My leaning is **B**, possibly C later, because the problem you're solving is ops reliability and B has the fewest failure modes. This is a finance/legal call, though. Some processors also offer longer authorizations for rental businesses. I believe card networks allow this for some rental categories, but I haven't verified it and don't know whether camera gear qualifies, so ask your processor before deciding. The booking and claim machines can be built in parallel. Only the definition of `SECURED` waits on this decision.

### 7. Timers

All durations are elapsed time, stored and compared in UTC and shown in the user's local time.

| ID | Timer | Value | Status |
|---|---|---|---|
| T1 | Owner acceptance | 24h from request, **or** pickup − X, whichever is earlier | Current rule is 24h. The pickup-sooner-than-24h case is undefined: **open Q** |
| T2 | Deposit authorization | At request if pickup is within the auth-safe window; otherwise at pickup − 48h, with retries; final failure at pickup − 24h | Proposed |
| T3 | Pickup window | Scheduled pickup + grace (proposed 12h) | **Open Q** |
| T4 | Return grace | Scheduled return + grace (proposed 2h) before `OVERDUE` | **Open Q**; ties to the late-fee policy |
| T5 | Damage report window | 48h from `returned_at` | Current rule |
| T6 | Renter response to a claim | Proposed 48h | **Missing from current rules** |
| T7 | Support decision | 5 business days from dispute | Current rule; define "business day" (support calendar, which time zone, holidays) |

### 8. Support overrides

Some cases will always need a person, such as a failed device at handover or proof of return after the fact. Support gets explicit override transitions with a required reason code and an evidence attachment. They are audited, reported weekly, and still subject to the money guards: an override cannot release a deposit while a claim is open. If overrides grow over time, that shows where the model is missing a transition.

### 9. Acceptance criteria (regression tests for last month's incidents)

1. **Given** a booking in `CLAIM_OPEN` (claim in `AWAITING_RENTER` or `UNDER_REVIEW`), **when** any actor or job tries to release the deposit, **then** the request is rejected and the deposit stays `SECURED`.
2. **Given** a secured deposit whose authorization expires in under 24h while the booking is not terminal, **when** the expiry monitor runs, **then** it re-secures the deposit per §6 or pages ops. It never lets the hold lapse silently.
3. **Given** a booking in `CONFIRMED` (never handed over), **when** the owner tries to mark it returned, **then** the action is not offered in the UI and the API rejects it with `illegal_transition`.
4. **Given** a booking in `CANCELLED`, `DECLINED` or `NO_SHOW`, **when** the late-fee job runs, **then** no fee is computed or charged. Late fees are computed only for time spent in `OVERDUE`.
5. **Given** any transition, **then** an audit record exists with actor, from/to state, UTC timestamp and reason.

### 10. Scope

**In scope (v1):** the three state machines; the transition service; handover/return codes; timers T1–T7; expiry monitor; audit log; ops UI showing only legal actions; support overrides.

**Out of scope (v1):** rental extensions and early-return refunds (both need new transitions, handled in v2); shipping/delivery; partial returns (e.g. body returned, lens missing); collecting damage above the deposit; insurance; changes to cancellation-fee amounts. For partial returns, v1 behavior is that the rental stays `ACTIVE`/`OVERDUE` and support handles it.

### 11. Rollout

| Phase | Scope |
|---|---|
| 0. Decide | §6 deposit strategy and the open questions in §12 |
| 1. Shadow | Run the state machine alongside the current system and log every event the new model would reject, without enforcing |
| 2. Audit | Map in-flight bookings to the new states; route ones that don't fit to support for manual cleanup |
| 3. Enforce | Turn on the guards; remove direct-write ops tools |

The phase 1 logs also give you the baseline you don't have yet: how often invalid actions happen today.

### 12. Success metrics

| Metric | Current | Target |
|---|---|---|
| Illegal transitions executed | Unknown (3 known incidents last month) | 0 (enforced by code) |
| Deposit holds lapsed while liability open | Unknown, check processor logs | 0 |
| Support overrides per 100 rentals | Unknown, measured from phase 1 | Tracked; investigate if rising |
| Claim decisions exceeding 5 business days | Unknown | 0 |

### 13. Open questions (owner in brackets)

1. Deposit strategy A/B/C [Finance, Legal]
2. What happens when pickup is less than 24h after the request? [Product, Ops]
3. Pickup grace and no-show fault: who pays what when the renter or the owner doesn't show? [Product, Ops]
4. Return grace and late-fee rate; the escalation threshold for unreturned gear [Product, Ops]
5. Renter silence on a claim: deemed accepted, or auto-sent to review? [Product, Support]
6. Can an owner cancel a `CONFIRMED` booking at any time before pickup, and what penalty applies? [Product]
7. Business-day calendar for T7 [Support]

---

If you answer questions 2–5, I can turn this into final user stories per state for the backlog. I'd send section 6 to finance now, because it's the only thing blocking the deposit work.


## Answer B
Below is a draft spec. Before the spec, there is one problem you should know about, because the state machine alone won't fix it.

**The deposit hold can't cover the whole rental as you described it.** Here is the longest path a rental can take: 14-day rental, then a 48h damage window, then the renter's dispute window, then 5 business days for support. That comes to roughly 3.5 weeks after pickup. You said a card hold lasts about 7 days. So a hold placed at pickup will expire in the middle of most long rentals and most claims. From the outside, an expired hold looks exactly like a deposit that was released. I'd check whether last month's "deposit released during an open claim" was actually a hold that expired. The fix is a payments decision (D1 below), not a state-machine rule. Also, the 7-day figure is your number. Real hold lifetimes vary by card network and processor, so confirm it with your payment provider before engineering designs around it.

---

# Spec: Rental Lifecycle State Model (v0.1 draft)

## 1. Problem

Ops and engineering don't share one definition of the states a rental can be in, which actions are allowed in each state, or who can take them. Actions that should be impossible still happen. Incidents last month:

| # | Incident | Likely root cause (to confirm from logs) |
|---|---|---|
| 1 | Deposit released while a damage claim was open | Deposit release doesn't depend on claim state, **or** the card hold expired silently |
| 2 | Owner marked "returned" on a rental that was never picked up | "Returned" can be set from any state, and there is no proof of pickup |
| 3 | Renter charged a late fee on a booking the owner had cancelled | The late-fee job was scheduled at booking time and doesn't re-check state when it runs |

All three have the same underlying cause: the status is a field anyone can edit, when it should be a state machine that enforces its own rules.

## 2. Solution overview

- **Three linked state machines**: Rental, Deposit and Claim. Each has one current state.
- **A single transition service**: every change goes through it. No direct edits to the status field by UI, jobs, ops tools or database scripts.
- **Guards**: each transition names the states it can start from, who can trigger it and the conditions that must hold.
- **Cross-machine invariants**: these make the three incidents impossible (Section 6).
- **An append-only event log**: every transition is recorded.

## 3. Scope

**In scope:** rental, deposit and claim states; transitions; timers; invariants; permissions for ops actions; migrating rentals that are in flight at launch.

**Out of scope (for this version):** cancellation fee amounts and refund policy; late-fee pricing; owner payout timing (dependency, see R-4); insurance or damage protection; delivery or shipping; lost or stolen gear beyond the NOT_RETURNED escalation; damage above the deposit amount.

## 4. State machines

### 4.1 Rental

| State | Meaning | Terminal? |
|---|---|---|
| REQUESTED | Renter booked; waiting for the owner | No |
| DECLINED | Owner declined | **Yes** |
| EXPIRED | Owner didn't respond within 24h | **Yes** |
| CONFIRMED | Owner accepted; gear not yet handed over | No |
| CANCELLED | Ended before pickup (by renter, owner or system). `cancel_reason` is required | **Yes** |
| ACTIVE | Handover at pickup confirmed by both parties | No |
| OVERDUE | Past `rental_end_at` + grace, not yet returned | No |
| NOT_RETURNED | Overdue past the escalation threshold; ops owns it | No |
| RETURNED | Return confirmed; 48h inspection window open | No |
| IN_CLAIM | Owner filed a damage claim within the window | No |
| CLOSED | Finished; money settled | **Yes** |

**Transitions**

| ID | From | Event | Actor | Guard | To | Side effects |
|---|---|---|---|---|---|---|
| R1 | none | Book | Renter | Dates available. If pickup is within the auth lead time (D6), the deposit auth must succeed | REQUESTED | Deposit → AUTHORIZED or SCHEDULED; start the accept timer |
| R2 | REQUESTED | Accept | Owner | now < `accept_deadline` | CONFIRMED | Block the calendar |
| R3 | REQUESTED | Decline | Owner | none | DECLINED | Release the deposit if authorized |
| R4 | REQUESTED | Accept timer fires | System | State is still REQUESTED | EXPIRED | Release the deposit if authorized |
| R5 | REQUESTED, CONFIRMED | Cancel | Renter / Owner | Before handover | CANCELLED | Release the deposit; `cancel_reason` = renter / owner |
| R6 | CONFIRMED | Auth deadline passes, deposit not AUTHORIZED | System | Deposit is AUTH_FAILED | CANCELLED | `cancel_reason` = deposit_failed |
| R7 | CONFIRMED | Pickup handover | Owner + Renter (handover code) | Deposit is AUTHORIZED and valid through the next re-auth point; now is inside the pickup window | ACTIVE | Record `picked_up_at` |
| R8 | CONFIRMED | Pickup window closes with no handover | System | State is still CONFIRMED | CANCELLED | `cancel_reason` = pickup_missed; ops assigns fault if either party reports |
| R9 | ACTIVE | End timer fires (`rental_end_at` + grace) | System | State is still ACTIVE | OVERDUE | Late fees start accruing |
| R10 | ACTIVE, OVERDUE | Return handover | Owner + Renter (handover code) | none | RETURNED | Record `returned_at`; stop late-fee accrual; start the 48h inspection timer |
| R11 | OVERDUE | Escalation timer fires (D5) | System | State is still OVERDUE | NOT_RETURNED | Alert ops; hold the deposit |
| R12 | NOT_RETURNED | Return handover | Owner + Renter | none | RETURNED | Same as R10 |
| R13 | RETURNED | Owner files a claim | Owner | now < `returned_at` + 48h | IN_CLAIM | Create the claim (REPORTED); capture the deposit per D1 |
| R14 | RETURNED | Owner confirms no damage | Owner | none | CLOSED | Release the deposit |
| R15 | RETURNED | Inspection timer fires | System | State is still RETURNED | CLOSED | Release the deposit |
| R16 | IN_CLAIM | Claim reaches SETTLED | System | Claim is SETTLED | CLOSED | Final settlement already done by the claim |

**How handover codes work.** At pickup, the owner's app shows a one-time code and the renter enters it in their app. At return, the roles are reversed. This proves both people were present and confirmed the handover. It directly prevents incident #2. If the app fails, ops can confirm the handover manually (Section 7).

### 4.2 Deposit

| State | Meaning |
|---|---|
| SCHEDULED | Booking is far out; auth planned for `pickup_at` − lead time (D6) |
| AUTHORIZED | Hold active. `auth_expires_at` is recorded |
| AUTH_FAILED | Auth or re-auth failed; renter asked to update their card before the deadline |
| CAPTURED | Funds taken, full or partial (claim, late fees, NOT_RETURNED) |
| RELEASED | Hold released or captured funds refunded. Terminal |
| LAPSED | Hold expired without the system acting. **This is an alarm state** (see I7) |

Only the transition service may release, capture or re-authorize a deposit, and only as a side effect of a Rental or Claim transition. **Nothing else in the system can release a deposit**: no button, no ops tool, no cron job.

### 4.3 Claim

The claim exists only after R13.

| From | Event | Actor | Guard | To |
|---|---|---|---|---|
| none | File (photos + amount required) | Owner | Rental is RETURNED and within 48h | REPORTED |
| REPORTED | Renter accepts | Renter | none | ACCEPTED |
| REPORTED | Renter disputes | Renter | now < `reported_at` + dispute window (D2) | DISPUTED |
| REPORTED | Dispute window expires | System | State is still REPORTED | ACCEPTED |
| REPORTED | Owner withdraws | Owner | none | WITHDRAWN |
| DISPUTED | Support decides: owner upheld / renter upheld / partial (amount) | Support | Reason note required | DECIDED |
| ACCEPTED, DECIDED, WITHDRAWN | Settlement executed | System | Payment succeeded | SETTLED |

The 5-business-day limit is an **SLA, not a timer that changes state**. If support misses it, the system escalates to a support lead. It never auto-decides and never releases the deposit.

## 5. Timers

- All timestamps are stored in UTC. Each timer is scheduled from a recorded timestamp: accept from `requested_at`, inspection from `returned_at`, and so on.
- **Every scheduled job carries the state it expects.** When it fires, it re-reads the current state. If the state doesn't match, the job does nothing and logs the mismatch. This rule alone would have stopped incident #3.
- A transition into a terminal state cancels pending jobs for that rental. The guard above is the real protection; cancelling jobs is only cleanup.
- "Business days" uses the support team's calendar, including holidays. Ops needs to own and publish this calendar (D10).

## 6. Invariants (must be enforced in code and tested)

| ID | Invariant | Prevents |
|---|---|---|
| I1 | Deposit funds are released only when the rental enters a terminal state, or when a claim settles. No release while the rental is ACTIVE, OVERDUE, NOT_RETURNED, RETURNED or IN_CLAIM | #1 |
| I2 | Terminal states (DECLINED, EXPIRED, CANCELLED, CLOSED) accept no further transitions. Corrections after closure are separate adjustment records, never a state change | #3 |
| I3 | RETURNED can only be entered from ACTIVE, OVERDUE or NOT_RETURNED | #2 |
| I4 | Late fees accrue only while the rental is OVERDUE or NOT_RETURNED. They are computed from `rental_end_at`, grace and `returned_at`, and charged only at R10 or R12 or by ops on NOT_RETURNED | #3 |
| I5 | A timer event is a no-op unless the current state matches the state it expects | #3 |
| I6 | A claim can be created only while the rental is RETURNED and now < `returned_at` + 48h | Late or invalid claims |
| I7 | While the rental is ACTIVE, OVERDUE, NOT_RETURNED, RETURNED or IN_CLAIM, the deposit must be AUTHORIZED with `auth_expires_at` > now, or CAPTURED. A LAPSED deposit in any of these states triggers a page to on-call | #1 (the hold-expiry variant) |
| I8 | One transition at a time per rental, enforced with an optimistic lock on a `version` column. Events are idempotent, keyed by `event_id` | Race conditions |
| I9 | Every transition writes an append-only record: from, to, event, actor, reason, timestamp | Auditability |

## 7. Ops permissions

Ops works **through transitions, with required reason codes**. Ops cannot edit the status field directly.

| Ops can | Ops cannot |
|---|---|
| Confirm a pickup or return handover manually (R7 / R10) with an evidence note | Release a deposit directly |
| Cancel a CONFIRMED rental (R5) with a reason | Move a rental backward, e.g. RETURNED → ACTIVE |
| Decide a disputed claim | Close a rental with an unsettled claim |
| Create post-closure adjustments (refund or credit) | Reopen a terminal rental |

Each ops action shows who did it and why in the rental's timeline. The timeline is visible to support.

## 8. Open decisions (each needs an owner before build)

| ID | Decision | Proposed default | Owner |
|---|---|---|---|
| **D1** | **How is the deposit covered beyond the hold lifetime?** Options: (a) re-authorize before each expiry (may fail; possible double holds; processor rules vary); (b) charge the deposit at pickup and refund at CLOSED (money is secured, but worse renter experience, refund delay, and processing fees may not be refundable, so verify); (c) short hold plus an off-session charge to the saved card for claims (may fail). | (a) during the rental, plus capture of the claimed amount at R13. If re-auth fails, ask the renter to fix it, then escalate to ops. This is a judgment call; payments engineering and the processor need to validate it | Payments + PM |
| D2 | Renter's dispute window after a claim is filed (not currently defined) | 72h | PM + Support |
| D3 | Pickup window and no-show fault/fees | Scheduled time + 4h; fault set by ops on report | Ops |
| D4 | Late-fee grace period and formula | Pricing's decision; this spec only defines when fees can accrue | Pricing |
| D5 | Threshold for escalating OVERDUE to NOT_RETURNED | 48h after `rental_end_at` | Ops + Trust & Safety |
| D6 | Deposit auth lead time for far-out bookings, and the deadline to fix a failed auth | Auth at T−48h; deadline at T−24h, then R6 cancels | Payments + Ops |
| D7 | Accept window when pickup is less than 24h away | min(24h, `pickup_at` − 2h) | PM |
| D8 | Can the owner cancel after acceptance, and with what penalty? | Allowed before handover (R5); penalty policy is out of scope | PM + Ops |
| D9 | Does one booking cover one listing or a bundle? Partial returns? | One booking = one listing (a kit counts as one) for v1 | PM |
| D10 | Business-day calendar and timezone for the support SLA | Support team's local calendar, published by ops | Ops |

## 9. Acceptance criteria (samples)

- **Incident #1.** Given a rental IN_CLAIM with the claim DISPUTED, when any actor or job attempts a deposit release, then it is rejected, and the attempt is logged with actor and source.
- **Incident #1, hold variant.** Given a rental ACTIVE with `auth_expires_at` within the re-auth lead time, when the re-auth job runs, then the deposit is re-authorized. If that fails, the deposit moves to AUTH_FAILED, the renter is notified and ops is alerted. The deposit never silently becomes LAPSED.
- **Incident #2.** Given a rental CONFIRMED, when the owner attempts to mark it returned, then the action is not available in the UI and the API rejects it with `invalid_transition`.
- **Incident #3.** Given a rental CANCELLED, when a late-fee job fires for it, then no charge is created and a `stale_timer` event is logged.
- **Claim window.** Given a rental RETURNED at time T, when the owner files a claim at T+48h+1min, then it is rejected, and the rental has already moved to CLOSED via R15.
- **Race.** Given a rental RETURNED, when the owner's claim (R13) and the inspection timer (R15) arrive together, then exactly one transition succeeds. The winner is whichever is first by `returned_at` + 48h cutoff; the other fails on the version check.

## 10. Success metrics

| Metric | Current | Target | When |
|---|---|---|---|
| Incidents of types #1 to #3 | Baseline from last quarter's ops tickets (not in hand) | 0 | 60 days after launch |
| Deposits reaching LAPSED while the rental is not terminal | Unknown; measure first | 0 | At launch |
| Rentals needing manual ops state correction | Baseline from tickets | −50% (placeholder; set after baseline) | 90 days |
| Invalid-transition rejections per week | New metric | Monitor; spikes mean a UX gap | Ongoing |

## 11. Risks and dependencies

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-1 | Re-auth failures leave deposits uncovered | Medium | High | D1 decision; alerting (I7); escalation to ops |
| R-2 | Handover codes add friction, and people skip them | Medium | Medium | Ops manual confirmation path; monitor how often it is used |
| R-3 | In-flight rentals don't map cleanly to the new states | High | Medium | Migration script flags inconsistent records for ops review before cutover |
| R-4 | Owner payouts are released on the old logic | Medium | High | Audit payout triggers; tie payouts to CLOSED in a follow-up |

## 12. Milestones

| Phase | Scope |
|---|---|
| 1. MVP | Transition service, the three machines, invariants I1 to I9, state-guarded timers, event log, migration of in-flight rentals, and ops tool changes. This stops #2 and #3, and #1 when the cause is claim logic rather than hold expiry |
| 2 | Handover codes for pickup and return |
| 3 | Deposit coverage per the D1 decision, plus re-auth and LAPSED alerting |

The defaults in Section 8 are my proposals, not validated policy. D1 and D2 block Phase 3 and the claim flow, so settle those two first. I'd also pull the event history for last month's three incidents before finalizing: if #1 turns out to be a hold expiry, Phase 3 moves up in priority.

