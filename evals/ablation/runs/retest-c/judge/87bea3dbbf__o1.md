# Case conversion-drop-mix-shift-en

## Conversation so far
### User
Our e-commerce site conversion rate dropped from 3.1% to 2.4% week over week. Nothing obvious changed. Where do I start?

### Assistant
A drop that sharp usually points to a funnel break. My top suspect is checkout: a payment-provider issue or a broken form on some browser. Check the checkout completion rate for both weeks.

## Latest user message
Checked. Checkout completion is 62% both weeks, basically identical. But add-to-cart rate fell from about 9% to about 7%, so I'm thinking something broke on product pages. Should I get QA to go through the PDP add-to-cart button on all browsers? For reference the dashboard shows sessions ~100k last week vs ~140k this week, orders ~3,100 vs ~3,360.

## Rubric (what a strong answer does)
1. Explicitly says the unchanged 62% checkout completion rules out checkout as the cause
2. Notices that orders rose while sessions rose ~40%, and explains the rate drop as added lower-converting traffic diluting the overall rate (a mix shift), which also accounts for the lower add-to-cart rate, rather than a broken product page
3. Does the arithmetic correctly: if the original ~100k sessions still converted at ~3.1% (~3,100 orders), the ~40k extra sessions produced ~260 orders, roughly 0.65% conversion
4. Proposes segmenting conversion by traffic source or channel before a browser QA sweep, and states that if the pre-existing channels still convert at ~3.1% the drop is explained

## Answer A
Not yet. Your own numbers point to a traffic problem, not a PDP bug. I'd hold off on the QA sweep until you've run one quick segmentation. (My checkout guess last turn was wrong too, and for the same reason: I looked at the rate before checking the volumes.)

**What the numbers say**

| | Last week | This week | Change |
|---|---|---|---|
| Sessions | ~100k | ~140k | **+40%** |
| Orders | ~3,100 | ~3,360 | **+8.4%** |
| Conversion | 3.1% | 2.4% | -0.7 pts |
| Add-to-carts (sessions × ATC rate) | ~9,000 | ~9,800 | +9% |
| Orders per add-to-cart | ~34.4% | ~34.3% | flat |

- **Orders went up.** The rate fell because the number of sessions it's divided by grew much faster than orders did.
- **Visitors who add to cart behave exactly as before.** Orders per add-to-cart is flat at about 34%, and checkout completion is flat at 62%. Nothing below the cart moved.
- **The extra sessions barely convert.** Suppose last week's ~100k-session baseline held steady. Then the extra ~40k sessions produced only about 260 orders (~0.65% conversion) and about 800 add-to-carts (~2% ATC rate). That's a rough estimate from your rounded figures, but the pattern is clear.

A broken add-to-cart button would push add-to-cart counts down, and it would normally show up in specific browsers or devices. What you have instead is add-to-carts rising in line with orders while a large new block of sessions converts at roughly a fifth of the usual rate. That is a change in traffic mix.

**Where to look next (about an hour in analytics)**

1. **Split sessions by source/medium and campaign, week over week.** Find where the +40k came from. Likely culprits: a new paid campaign, an affiliate, a social post that took off, an email to a cold list, or referral spam.
2. **Check for bots.** Look for sessions lasting 0–2 seconds, single-page sessions, unusual user agents or data-center locations, a spike from one country or city, and odd hour-of-day patterns.
3. **Check for tracking changes.** Look at tag or GTM edits, a consent-banner change, duplicate tags firing, a changed session timeout, or a new subdomain or payment redirect that splits sessions. Any of these can inflate sessions while the site itself is fine. "Nothing obvious changed" often means nothing obvious to the people you asked, and marketing or analytics may have changed something.
4. **Confirm by comparing like with like.** Take the segments that existed last week (e.g., returning visitors, organic, direct) and compare their conversion and ATC rates across the two weeks. If they're roughly flat (~3.1% conversion, ~9% ATC), nothing on the site broke.

**When a PDP check would make sense**

Only if step 4 shows ATC falling *within* stable segments, or falling for one browser or device in particular. Then have QA target that specific combination, not every browser.

**Caveat**

All of this uses your rounded dashboard figures, so treat the derived numbers (0.65%, 2%, 34%) as approximate. The conclusion holds unless the real figures differ a lot from the rounded ones.

If the new traffic turns out to be real (a campaign, say), the next question isn't "what broke" but "is it worth what we pay for it". Judge that on revenue and cost per order for that channel, not on the blended site-wide conversion rate.


## Answer B
I'd hold off on the QA sweep for now. Your own numbers suggest nothing broke on the product pages. What changed is **who came to the site**. Also, my first guess of a funnel break was the wrong starting point: I should have asked for the raw counts before the rates.

### Look at counts, not only rates

| | Last week | This week | Change |
|---|---|---|---|
| Sessions | ~100,000 | ~140,000 | **+40%** |
| Add-to-carts (sessions × ATC rate) | ~9,000 | ~9,800 | +9% |
| Orders | ~3,100 | ~3,360 | **+8.4%** |
| Conversion rate | 3.1% | 2.4% | -0.7 pp |
| Orders per add-to-cart | ~34.4% | ~34.3% | flat |

- **Orders went up.** If add-to-cart were broken on some browsers, you'd expect fewer carts and fewer orders. Both rose about 8–9%.
- **Everything after add-to-cart is unchanged.** Orders per cart held at about 34%, and checkout completion held at 62%.
- **The rates fell because the denominator grew.** Sessions jumped 40%, but carts and orders grew only about 9%. Suppose your usual traffic converted at 3.1% as it did last week. Then the extra ~40k sessions produced only about 260 orders, which is roughly **0.6–0.7% conversion**. That is an assumption, but it's the simplest explanation that fits all your numbers.

So the question to answer is: **where did the extra ~40k sessions come from, and are they real shoppers?**

### What to check, in order

1. **Break sessions down by source/medium, week over week.** Find the channel that grew by about 40k. Common causes are a new paid campaign, a broad or low-intent ad audience, an affiliate or social post, an email to a cold list, or a press or referral spike.
2. **Check for bot or junk traffic.** Look inside the new segment for near-zero session duration, single-page sessions, a spike from one country, ASN/data-center IP range or user agent, or direct traffic landing on odd URLs.
3. **Check for a tracking or measurement change.** Look at tag deploys, duplicate analytics tags, a changed session timeout, consent-banner or consent-mode changes, or cross-domain or payment-redirect settings that split one visit into several sessions. "Nothing obvious changed" on the site doesn't rule out a change in the analytics setup.
4. **Compare conversion for the traffic you already had.** Filter to the channels and landing pages that existed last week. If they still convert at about 3.1% with about 9% add-to-cart, the site is fine and the drop is all mix. If they also fell, the PDP theory is back on the table.
5. **Only then run QA, if step 4 shows a drop.** Cut add-to-cart rate by browser, device and landing page. Test the specific combination that dropped, not every browser.

### Caveats

- You gave rounded numbers ("about 9%", "about 7%"), so treat the derived figures as approximate. The direction is clear, though: orders and carts rose while sessions rose much more.
- One week of comparison can include seasonality or promo effects. If you have it, compare against the same week last year or a 4-week baseline.
- I can't see channel data, so the "new low-intent traffic" explanation is the most likely one, not a confirmed one. Step 1 will confirm or rule it out quickly.

For the business, the question to raise is whether the new traffic is worth its cost, not what broke. If it's paid, check its cost per order.

