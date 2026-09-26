Here's the budget first. Your 12 minutes are really **10 minutes of talking**, because Q&A takes the other 2. I've planned **9:30 of content** and left **0:30 of buffer**. You said you run over, so the buffer goes in on purpose. Don't spend it early.

**Audience:** DevOps practitioners at a meetup. They're technical and a bit skeptical ("sure, but *your* monorepo isn't mine"), and they want mechanics and scars, not slogans. They'll probably forgive a missing slide faster than a missing number.

**Core message:** *CI speed comes from running less, running in parallel and reusing work. Every shortcut you cache is a shortcut you have to be able to invalidate.*

**Structure:** Hook, then Before, then four fixes, then the backfire, then the takeaway. I tease the Docker story in the first 45 seconds (a curiosity gap) so the whole room waits for it.

---

## Time budget at a glance

| # | Slide | Duration | Starts at | Ends at |
|---|-------|----------|-----------|---------|
| 1 | 41 → 9 (hook + intro) | 0:45 | 0:00 | 0:45 |
| 2 | The before state | 1:00 | 0:45 | 1:45 |
| 3 | Sharding across 8 runners | 1:00 | 1:45 | 2:45 |
| 4 | Dependency caching | 1:15 | 2:45 | 4:00 |
| 5 | Change detection | 1:15 | 4:00 | 5:15 |
| 6 | Flaky-test quarantine | 1:00 | 5:15 | 6:15 |
| 7 | The backfire: stale base image | 1:30 | 6:15 | 7:45 |
| 8 | How we fixed the cache | 1:00 | 7:45 | 8:45 |
| 9 | Takeaway + ask | 0:45 | 8:45 | 9:30 |
| – | Buffer (don't plan to use it) | 0:30 | 9:30 | 10:00 |
| – | Q&A | 2:00 | 10:00 | 12:00 |
| | **Total** | **12:00** | | |

Your requirements, checked against the table:
- Intro is 0:45, under the 1:00 cap.
- The Docker story (slides 7 and 8) runs 2:30, over the 2:00 minimum.
- Q&A gets its full 2:00.
- Everything adds up to exactly 12:00.

---

## Slide 1: "41 → 9"
**Type:** Title-Only
**Visual:** Two giant numbers, "41 min" crossed out and "9 min" next to it. Nothing else. Your name and handle go small in a corner, and no "about me" slide.
**Key Message:** We made CI more than 4x faster, and one of the tricks bit us.
**Speaker Notes:** "Our CI used to take 41 minutes per PR. Today it takes 9. I'll show you the four changes that got us there. I'll also show you the one that shipped a stale image to staging, because that's the part you should actually steal the lesson from."
**Attention Hook:** A curiosity gap. You've promised a failure story, so they'll wait for it.
**Timing:** 0:45

## Slide 2: "1,800 tests. One line."
**Type:** Data-Driven
**Visual:** A single long horizontal bar labeled "41 min", made of 1,800 tiny test blocks in one serial lane. Caption: "Monorepo. Every PR. Every test. Serially."
**Key Message:** The pipeline was slow because it did everything, in order, every time.
**Speaker Notes:** "This was the setup: a monorepo, with all 1,800 tests running one after another on every PR, whether you touched a README or the payments service. Developers had learned to open a PR and go get lunch. The problem wasn't slow tests. It was that we ran all of them, one at a time."
**Timing:** 1:00

## Slide 3: "Split it 8 ways"
**Type:** Data-Driven
**Visual:** The same bar from slide 2, now chopped into 8 parallel lanes. Put the resulting wall-clock time for this step above it.
**Key Message:** Parallelism is the cheapest big win.
**Speaker Notes:** "First move: shard the suite across 8 runners, so roughly 225 tests each. Say how you split them: by count, by historical timing, or by file. Timing-based balancing usually matters more than people expect, because one fat shard sets your wall-clock time. Name the one gotcha you hit, such as shared state between tests or uneven shards."
**Timing:** 1:00
**Fill in:** your real number for what sharding saved. I don't have your per-step breakdown, so don't let me invent one.

## Slide 4: "~6 minutes we were re-downloading"
**Type:** Data-Driven
**Visual:** A before/after bar for the dependency-install step only, with "−6 min" in large type.
**Key Message:** Caching dependencies saved about 6 minutes on its own.
**Speaker Notes:** "Every run reinstalled the whole dependency tree from scratch. We cached it, keyed on the lockfile, and that alone saved about 6 minutes. Remember this slide. The idea that cache keys decide correctness comes back in two minutes, and it won't be friendly."
**Attention Hook:** A deliberate plant. That line sets up the Docker payoff.
**Timing:** 1:15
**Check:** I assumed the cache key is the lockfile hash. Swap in whatever you actually keyed on.

## Slide 5: "Don't test what you didn't touch"
**Type:** Problem-Solution
**Visual:** On the left (red), a PR touching one package with all packages lit up. On the right (green), the same PR with only that package and its dependents lit.
**Key Message:** The fastest test is the one you don't run.
**Speaker Notes:** "Next, change detection. We compute which packages a PR affects, including downstream dependents, and test only those. Say how you build the dependency graph and what you do when shared config or root files change. That edge case is where people get burned, and this audience will ask about it. Give the typical PR's new time if you have it."
**Timing:** 1:15

## Slide 6: "Flaky tests go to jail"
**Type:** Problem-Solution
**Visual:** On the left, a red "Retry" button with a click counter. On the right, a green "Quarantine" lane running beside the main pipeline and not blocking it.
**Key Message:** Flaky tests cost more in retries and lost trust than in runtime.
**Speaker Notes:** "Once CI got fast, flakes became the bottleneck, because a 9-minute pipeline you re-run three times is a 27-minute pipeline. We set up a quarantine: a test that flakes gets moved out of the blocking path and tracked until someone fixes it. Say how a test gets in, how it gets out, and who owns it. Without an exit rule, quarantine turns into a graveyard."
**Timing:** 1:00

## Slide 7: "And then staging got a ghost"
**Type:** Story-Driven
**Visual:** Full-bleed image of your staging environment or deploy log, or a screenshot of the Slack message where someone first noticed, with names redacted. One caption: "The image was fresh. The base wasn't."
**Key Message:** Aggressive layer caching shipped a stale base image to staging.
**Speaker Notes:** Tell this as a scene, in order, and slow down. "We were caching Docker layers hard, and it worked beautifully until [day/moment]. Someone noticed [symptom]. We spent [time] assuming it was our code. It turned out our cached layers were still built on an old base image. The upstream image had moved, but our cache key never saw it." Pause for 2 seconds before "our cache key never saw it". That line is the whole talk.
**Attention Hook:** A story and a pattern break. Step away from the laptop and just tell it. It lands near the 6-minute mark, which is where attention starts to sag.
**Timing:** 1:30

## Slide 8: "Your cache key is a correctness decision"
**Type:** Problem-Solution
**Visual:** On the left (red), the old cache key. On the right (green), the new one with the base-image digest (or whatever you added) highlighted.
**Key Message:** Every cache needs an invalidation story you can explain in one sentence.
**Speaker Notes:** "Here's what we changed." Describe your actual fix. Common options are pinning the base image by digest, putting the base digest into the cache key, a scheduled forced rebuild, or always pulling the base before building. I'm guessing at which of these you did, so use the real one. "Back on slide 4 I said the cache key decides correctness. Dependencies taught us that the easy way. Docker taught us the hard way."
**Timing:** 1:00

## Slide 9: "Run less. Run parallel. Reuse carefully."
**Type:** Title-Only
**Visual:** Those three phrases, with "carefully" in a different color. "41 → 9" goes small in the corner, echoing slide 1.
**Key Message:** The four wins and the one warning in a single line.
**Speaker Notes:** "So: shard, cache, skip what didn't change, quarantine the flakes. And for every cache you add, write down what invalidates it. My ask: tomorrow, open your slowest pipeline and find the one stage that runs everything every time. That's your 41 minutes. Questions?"
**Timing:** 0:45 (and you're into Q&A at 9:30, with 30 seconds in hand)

---

## Delivery notes for someone who runs over

- **Use checkpoints, not a clock you glance at nervously.** Memorize three times. At **2:45** you start caching. At **6:15** you start the Docker story. At **8:45** you start the closer. If you're 30+ seconds late at 6:15, cut from slides 5 and 6 on the spot and **never from Docker**.
- **Plan your cuts in advance.** If you're behind, drop the dependency-graph detail on slide 5 and the ownership detail on slide 6. Together that's about 30 seconds.
- **Don't take mid-talk questions.** Say "hold that for Q&A" and keep going. One early question can easily cost you more than your whole buffer.
- **Rehearse the Docker story out loud at least twice with a timer.** Stories are exactly the part that swells on the day, because you remember one more detail.
- **Start slide 1 with the number, not with "Hi, so, um, I'm..."** Your name is already on the slide.

## "How to Fail" Corner

- Want to blow the 1-minute intro cap? Open with your company's founding story and a team photo, while the organizer glares at you from the back.
- Want the Docker story to fall flat? Put it on one bullet ("cache issue, fixed w/ digest pinning") and rush through it because you're 90 seconds over. That's the part people came to hear.
- Want nobody to trust your 41 → 9? Skip the per-step numbers and let the audience wonder whether 20 of those minutes came from a bigger runner size.
- Want Q&A to vanish? Treat the 30-second buffer as bonus content time. It isn't.
