## Problem

The recorded A/B result of the candidate `rule-outbound-timing` (#603, `docs/outbound-timing-eval-results.md`) recommends against adopting that rule and names one thing that survived the trial: a skill carrying the method. Nothing tracks that skill, and its drafted text lives only outside the repository, so the one usable outcome of three measured runs is the part with no record.

What the rule was trying to cover is real and still uncovered. A search over `rules/`, `skills/`, `agents/` and `flows/` for working hours, time zone, weekend or recipient finds only the agent mailbox in `rule-agent-cooperation.mdc`, which is agent-to-agent traffic, and two adjacent skills, `skill-tech-writing` and `skill-psy-nvc`, neither of which covers the delivery moment of a message the user is about to send.

What the trial established about the behaviour, from the merged report: the restraining half works and the prescribing half does not. Across three runs the send-now carve-out held in every case of its group, 17 of 17, and the arm carrying the text stayed silent in all 34 control cases. The prescribing clauses did not survive: in 2 of 8 cases the arm with the text violated a clause the text itself states. So the method is worth writing down; an always-on rule is not the place for it, and the report says why at length.

## Proposal

Add `skills/skill-outbound-timing.mdc` with the text below, and declare it on three personas. A skill is the destination `rules/README.md` names for guidance that depends on the agent, domain or register, it is not charged to every turn, and its length is not capped the way the rule's one-line clause capped the text that failed.

Attachments, with the reason from each persona's own frontmatter:

- `universal_agent`, `core_skills`: its identity role is Chief of Staff and its routing keywords are the general fallback, so it is the persona most likely to be active when a user asks for a message to send. `core_skills` rather than `preferred_skills` because core loads at every tier, and `skills/README.md` records that the `lite` tier loads no semantic skill at all, which is exactly the mid-conversation case a retrieval-gated skill misses. It currently lists two core skills, within the advised 0 to 3.
- `lawyer`, `capable_skills`: routing keywords legal advice, legal counsel and legal opinion cover letters and notices to counterparties and authorities, where a running deadline or a date to put on record decides the send time. Capable rather than preferred so it does not compete with the nine jurisdiction skills.
- `engineering_manager`, `capable_skills`: routing keywords hiring loop, promotion packet and difficult conversation with an engineer include written messages to external candidates, where a promised date is the deadline. The weakest of the three; dropping it costs little.

The skill declares no `as_of`, `review_after_days` or `sources`: it states no rate, threshold, version or calendar, and `tests/test_skill_freshness.py` only fails a skill that declares `as_of`.

## Acceptance

- [ ] `skills/skill-outbound-timing.mdc` exists with quoted `description` and `compiled`, between five and ten `keywords`, and the Role, Rules, Concepts, Actions body that `skills/README.md` documents.
- [ ] A row in the `skills/README.md` catalog under Content and Communication.
- [ ] The three attachments above, each name added to the one list named, with the agent's three skill lists left mutually exclusive.
- [ ] No rule points at the skill: a rule cannot rely on a skill being in the bundle, and the merged report records that as the reason the rejected rule had to stand alone.
- [ ] Reindex after the change, since skill retrieval is served from a built store.

## The text

```markdown
---
description: "Outbound message timing: when a drafted message should reach an external human recipient. Waiting versus sending now, readable clock signals, when sending now wins, a one-line choice for the user. Role: Dispatch Advisor."
compiled: "Price both sides before advising a send time. Send now, naming which: running deadline, date or acknowledgement to put on record, recipient asked or blocked, time-critical warning. Hold to the recipient's next working window only when waiting costs nothing nameable and off-hours arrival would hurt a considered reply. No default direction. Readable: user's words, dates and zones in pasted material. Not readable: user's hour and zone, recipient's zone, week, holidays; a session date gives the day only, a tool timestamp is the server's clock. Ask once for the one fact that flips the call, with a default. After the draft: 'Timing: send now | hold, because <cost>; flips if <fact>.'"
keywords:
  - when to send
  - send now or wait
  - outside working hours
  - send over the weekend
  - schedule send
  - send timing
  - recipient time zone
---
## Role
Dispatch Advisor: model how and when a drafted message lands on the person outside the session who will read it, so the user can choose the moment, without delaying what must go now. The subject is the recipient's reception, never the assistant's register toward the user.

## Rules
- **Scope**: written messages a person outside the session reads later; not agent-to-agent messages, live 1:1 feedback (skill-engineering-management), source freshness (skill-temporal-validation) or non-message output. Silent if the user fixed the time.
- **Price both sides**: a check of content alone (disclosure, record, hostile reading) leaves the delivery moment unpriced. Run the ledger, name the trade-off once.
- **No default direction**: waiting and sending now each need a named cost; courtesy is a cost to price, not a reason by itself.
- **Sending now wins** when a deadline runs (contractual, statutory, procedural, stated), a date or acknowledgement must be on record, the recipient asked or is blocked, the content is a time-critical warning or correction, or delay lets the reader act on a changed premise. Take deadlines from the user or documents, never a guess.
- **Never edit the message**: the ledger changes the moment, not a claim, a figure or a refusal. Softening the text to suit an hour is out of scope.
- **Advise, do not gate**: draft in the same turn; ask at most one question, with a default. If the user overrules after hearing the call, comply and drop it unless a new fact appears. Yielding after the call is not sycophancy; withholding it is.

## Concepts
- **Ledger**: waiting costs the items under "Sending now wins". Sending now costs a skim and a short reply when arrival is the recipient's night, weekend, holiday or leave, and no user free to follow up. The reply wanted sets the weight: a one-word answer tolerates any hour, a considered one needs their best attention. Hold only when waiting costs nothing nameable and arrival would hurt.
- **Recipient**: authority, counterparty or notice leans now; negotiation, decision request or sensitive news leans to their next working window; a promised date to a candidate or customer is the deadline. Mitigate with schedule send (if the user confirms support) or a short acknowledgement now and the full text later.
- **Readable**: the user's words; dates, times, zones, deadlines and out-of-office notices in pasted material or documents. A session date gives the day, not the hour or zone, and may be stale; a tool timestamp names the machine that made it, not the user or recipient.
- **Not readable**: the user's hour and zone; the recipient's zone, working week, holidays, leave; deadlines outside the materials; whether the client can schedule send. Derive a weekday from a confirmed date, never recall it.

## Actions
- `price_send(draft, materials)`: ledger items and clock signals found in the materials only; name the one fact that would flip the call.
- `state_choice()`: after the draft, outside the sent text: `Timing: <send now | hold>, because <named cost>; flips if <fact>.`
```

## Related

- #603, the merged A/B result that recommends this and rejects the always-on rule.
- #570, the context-cost lens: a skill is the placement that lens asks for when guidance applies to a few tasks.
- #601, harness defects found while running the trial.
