Before the PRD, one thing to push back on: "we need notifications" is a solution, not a problem. If you bring a full build plan to sprint planning on Monday, the team may spend a quarter on a notification system without anyone knowing what it is meant to fix. The PRD below is a real draft you can bring. It frames the work as **a small discovery spike plus a narrow MVP**, and it marks every point where you need answers from leadership or data you don't have yet. I haven't made up any numbers. Every metric baseline is a placeholder for you to fill in.

The most important question to settle, ideally before Monday, is **who the notifications are for**:

- **Staff-facing** (front desk, dentists): in-app or email alerts about schedule changes, cancellations, lab results, tasks and so on. This is lower risk and fully inside your product.
- **Patient-facing** (appointment reminders, recall notices by SMS or email): probably more commercial value, but it brings in HIPAA (PHI in messages, BAAs with SMS and email vendors), TCPA consent rules for texts, and possible overlap with reminder tools clinics already pay for.

These are two different products. The draft assumes staff-facing for the MVP, because you can ship and learn from it faster. If leadership meant patient reminders, change the Solution and Scope sections, and plan for compliance review before any build.

---

## PRD: Notifications (v0.1 draft, for discussion)

### Problem
**Hypothesis (not yet validated):** Front desk staff and dentists miss time-sensitive events in the platform, such as same-day cancellations, late or no-show patients, schedule changes made by colleagues, and pending chart items. The only way they find out today is by manually checking screens. The result is idle chair time, double-booking and delayed follow-ups.

**Evidence we have:** Leadership asked for notifications in the quarterly review. The *why* behind that request isn't documented yet.

**Evidence we need (before committing beyond the MVP):**
- What triggered leadership's request: customer escalations, churn reasons, lost deals, a competitor feature?
- Support tickets and CSM notes that mention "didn't know", "missed", "wasn't told" or "reminder" (search the ticket system).
- 5–8 short calls with front desk leads at clinics of different sizes.
- Product analytics: how often users refresh or re-open the schedule view per day. A high number suggests they are polling for changes.

**JTBD (to validate):** "When something changes in today's schedule, I want to know right away without watching the screen, so that I can fill the gap or adjust before it costs the clinic time."

### Solution (high level)
Phase 0 is a 1-sprint discovery spike: answer the questions above and confirm the top 2–3 events that are worth notifying about.

Phase 1 (MVP) is an **in-app notification center** (bell icon, unread count, list of events) for a small, fixed set of high-value events. Each notification links to the relevant record. There is a simple per-user on/off setting for each event type.

Deliberately *not* in the MVP: email, SMS, push, digests, or a rules engine. Add channels only after in-app usage shows which events people actually act on.

### Success Metrics
| Metric | Current Value | Target | Deadline |
|--------|--------------|--------|----------|
| % of active clinics with ≥1 user opening a notification weekly | N/A (new) | TBD after spike (suggest setting it once we have 2 weeks of beta data) | MVP + 6 weeks |
| Notification click-through rate (opened → acted on the linked record) | N/A | TBD | MVP + 6 weeks |
| Time from cancellation to slot rebooked (if cancellation is an MVP event) | **Unknown, needs a baseline from existing data** | TBD | Next quarter |
| % of users who turn off all notifications (guardrail for noise) | N/A | Keep low; set a threshold after beta | Ongoing |
| Support tickets about missed schedule changes | **Unknown, pull a baseline** | Decrease | Next quarter |

Note: the business outcome leadership probably cares about (retention, utilization, expansion) needs to be named explicitly. Ask them which one.

### Scope
**In scope (MVP):**
- In-app notification center for staff users (front desk and dentist roles)
- 2–3 event types, chosen during the spike. Likely candidates: same-day cancellation, new booking or reschedule on my schedule, patient checked in or running late
- Read/unread state, mark all read, link to source record
- Per-user toggle for each event type
- Notification content with minimal PHI (e.g., "Cancellation at 2:30 PM, Chair 3" rather than the patient's full name and procedure). Confirm the approach with compliance.

**Out of scope (this phase):**
- Patient-facing notifications (SMS or email reminders, recalls)
- Email, SMS, mobile push and desktop/browser push channels
- Custom notification rules, escalations or scheduled digests
- Clinic-admin-level notification policies
- Integrations with third-party reminder or communication tools

### User Stories
1. As a **front desk staff member**, I want to be notified when a patient cancels an appointment for today, so that I can offer the slot to someone on the waitlist.
   - AC: Given a same-day appointment exists, When it is cancelled by anyone (staff or patient-facing channel), Then all front desk users at that clinic see a notification within [X seconds, to be set with engineering] linking to the open slot.
2. As a **dentist**, I want to be notified when an appointment on my schedule is added, moved or cancelled, so that I'm not surprised between patients.
   - AC: Given I am the assigned provider, When an appointment on my schedule changes, Then I receive a notification showing the time and type of change, and no one else's schedule changes notify me.
3. As a **user**, I want to turn off notification types I don't need, so that the notifications I keep stay useful.
   - AC: Given I open notification settings, When I disable an event type, Then I receive no new notifications of that type, and the setting persists across sessions and devices.
4. As a **user**, I want to see which notifications I haven't read, so that I don't miss something while I was with a patient.
   - AC: Given I have unread notifications, When I view any page, Then the bell shows an unread count. When I open a notification, Then it is marked read.

### Risks and Dependencies
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| We build something other than what leadership meant (e.g., they meant patient reminders) | Medium–High until clarified | High | Confirm the audience and the underlying goal with leadership before sprint commitment |
| Notification fatigue: too many alerts, users turn them all off | Medium | High | Start with 2–3 events, per-type toggles, and track the opt-out rate as a guardrail |
| PHI appears in notification content or logs | Medium | High | Minimum-necessary content, compliance review of copy and storage, and no external channels in the MVP |
| Event plumbing: the scheduling system may not emit reliable change events today | Unknown | High | Ask engineering during the spike whether a domain-event or change-log mechanism exists. Effort depends heavily on this |
| Real-time delivery requires new infrastructure (websockets/SSE vs polling) | Unknown | Medium | Engineering to choose. Polling at short intervals may be acceptable for the MVP |
| Opportunity cost: this displaces other roadmap items this quarter | High | Medium | Make the trade-off explicit to leadership: name what moves out |

### Milestones
| Phase | Scope | Deadline |
|-------|-------|----------|
| Phase 0: Discovery spike | Leadership clarification, ticket/analytics review, 5–8 clinic calls, engineering feasibility on event infrastructure, final choice of MVP events | Sprint 1 |
| Phase 1: MVP | In-app center + 2–3 events + toggles, beta with 10–20 clinics | Sprints 2–4 (estimate pending engineering) |
| Phase 2: Evaluate | Review metrics, decide on next channels (email/push) or patient-facing work | Beta + 4–6 weeks |

---

### What to actually do before Monday
1. **Send leadership one message:** "When you said we need notifications, what problem were you seeing: missed schedule changes for staff, patient reminders, or something customers asked for? And what outcome should it move?" Their answer decides which version of this PRD you're working on.
2. **Pull a quick ticket search** for "missed", "notify", "alert" and "reminder" so you have at least some evidence in the Problem section.
3. **At sprint planning, ask for the Phase 0 spike, not a build commitment.** Ask engineering one specific question: "Does scheduling emit change events today, or would we need to add that?" That answer probably decides the MVP effort more than anything else.

If leadership comes back saying they meant patient reminders, tell me and I'll rewrite this around that. The scope, risks (especially HIPAA/TCPA) and metrics would change a lot.
