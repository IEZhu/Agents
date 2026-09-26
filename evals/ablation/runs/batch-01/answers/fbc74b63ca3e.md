I'll rewrite the critic prompt, but I don't think a stingy critic is the main problem, and a friendlier critic on its own probably won't fix what your traces show.

## What the traces are telling you

Scores go 6, 7, 8, 8, 7, 8, 8, 7, 8 while the drafts get worse. That is not a critic that is stuck at a fixed low bar. It's a loop with no stopping point, fed by a critic that is told to always find more to fix:

1. **"List everything that could be improved" can never run out.** Any text can be improved somehow, so every round brings new feedback. The writer handles it by adding: more qualifiers, more benefits, more adjectives. That's where the adjective soup comes from. The critic is doing what it was told.
2. **There is no rubric, so the score is noise around roughly 7.5.** Without anchors, GPT-4o picks a number from its overall impression. The 7/8 wobble is sampling variance, not a real change in quality. Waiting for a 9 from a noisy scorer amounts to waiting for a lucky roll.
3. **The loop only exits on success.** There's no round cap, no plateau check, and no "keep the best draft". So when the loop fails, you ship the last draft, and after 40 rounds of adding things that's close to the worst one.
4. **The critic only sees the current draft.** It can't notice that round 12 is worse than round 3, so the process can't correct itself.

Suppose you just make the critic more generous. You'd likely get more 9s, including 9s for bloated drafts, and you'd lose the signal entirely. Calibration (a rubric plus a test set) is the fix. Leniency is not.

## Rewritten critic prompt

```
## Role
You are an e-commerce copy editor. You decide whether a product description is ready to publish, and if not, name the few changes that matter most.

## Input
- Product data (source of truth): {product_facts}
- Description to evaluate: {draft}
- Best previous version, if any: {best_previous_draft}

## Rubric (score each 0-2)
1. Accuracy: every claim is supported by the product data; nothing invented.
2. Clarity: a shopper understands what it is and who it's for in the first sentence.
3. Benefits: key features are tied to concrete buyer benefits.
4. Concision: no filler, stacked adjectives, or repeated points. Longer is NOT better.
5. Tone & format: matches brand voice {brand_voice}; within {min_words}-{max_words} words.

Total = sum x 1, max 10.

## Anchors
- 9-10: Publishable as-is. Remaining issues are matters of taste.
- 7-8: One or two concrete problems that a shopper would notice.
- 5-6: Missing key info, unclear, or noticeably padded.
- 0-4: Inaccurate, misleading, or unusable.

## Rules
- Judge against the rubric, not against an imagined perfect description. If all five criteria are met, the score is 9 or 10. Do not withhold a 9 because "it could always be better".
- Any unsupported claim caps Accuracy at 0.
- Give AT MOST 3 fixes, ordered by impact. Each fix must be specific (quote the text, say what to do). Prefer cutting over adding.
- If the score is 9 or higher, return no fixes.
- If a best previous version is provided, state whether this draft is better, worse, or equivalent. If worse, say why.

## Output (JSON only)
{
  "criteria": {"accuracy": 0-2, "clarity": 0-2, "benefits": 0-2, "concision": 0-2, "tone_format": 0-2},
  "score": <sum>,
  "vs_previous": "better" | "worse" | "equivalent" | "n/a",
  "fixes": ["...", "...", "..."]
}
```

Why these pieces:
- **The rubric and anchors** turn the score from vibes into something you can check, and let you see which criterion is holding a product back.
- **The 3-fix cap and "prefer cutting"** go straight at the adjective growth.
- **"Longer is NOT better" inside Concision** lets the critic push back on bloat, where before it rewarded it.
- **The comparison with the best previous draft** lets the critic see regressions.
- **Computing the score from the criteria** cuts down on the 7/8 flip-flopping. Also run the critic at temperature 0, and use structured output (`response_format` with a JSON schema; that's from memory, so check the current OpenAI docs).

## Fix the loop too (more important than the prompt)

```python
MAX_ROUNDS = 4
best = None
stale = 0
for i in range(MAX_ROUNDS):
    draft = writer(facts, feedback) if i else writer(facts)
    review = critic(facts, draft, best.draft if best else None)
    if best is None or review.score > best.score:
        best, stale = Result(draft, review.score), 0
    else:
        stale += 1
    if review.score >= 9 or stale >= 2:
        break
    feedback = review.fixes
ship(best.draft)          # always the best version seen, never the last
flag_for_review_if(best.score < 8)
```

- **Round cap:** your traces show the gains end by round 3, so 3-4 rounds is plenty. Check that on your own data.
- **Plateau exit:** stop after 2 rounds with no improvement.
- **Keep the best draft:** this alone would stop the late drafts from being worse than round 3.
- **Send low scorers to a person** instead of looping. Some products just have thin source data, and no amount of rewriting will produce a 9.

## Check it, don't assume it

Before you roll it out, have a person label about 30 SKUs (a mix of good, mediocre and bloated descriptions) as publishable or not. Then check:
- How often the critic's 9+ matches "publishable", including false 9s. That's the risk of an overly generous critic.
- How many rounds products take on average, and what share hit the cap.
- The average word count of shipped descriptions before and after. It should go down.

Change one thing at a time: first the loop (cap + best-so-far), then the critic prompt, so you know which change made the difference.

## Side note: the nightly job dying

This is the same kind of fix as the loop above: set limits and save your progress.
- **Save progress per SKU:** write each finished SKU (for example to a DB row or JSONL) as it completes, and on restart skip the ones already done. A timeout then costs you one SKU, not the whole night.
- **Retry each call** with exponential backoff (for example 3 attempts). If it still fails, mark the SKU as failed and move on instead of crashing.
- Set a per-request timeout, and consider the OpenAI Batch API for a nightly job of about 2,000 items. As I recall it's cheaper and built for async bulk runs, but check its current terms and turnaround window.
