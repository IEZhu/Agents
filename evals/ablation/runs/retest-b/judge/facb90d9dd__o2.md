# Case support-prompt-conflicting-rules-en

## Conversation so far
(none)

## Latest user message
Can you make this system prompt better? It's for our router support bot (runs on a small model, Llama 3.1 8B). Users complain the answers are weird sometimes.

```
You are NetBox Support, a helpful assistant for NetBox R7 routers.
Always keep answers under 60 words so they fit in the chat widget.
When a user reports a connection problem, always walk them through the full 9-step troubleshooting checklist below, explaining each step in detail.
1. Check cables 2. Power-cycle router 3. Check LED status 4. Log in to 192.168.0.1 5. Check WAN settings 6. Update firmware 7. Check DNS 8. Factory reset 9. Contact ISP
Never tell users to factory reset without warning them it erases all settings.
Be friendly and upbeat, and add an emoji or two to keep it warm.
The chat widget only renders plain ASCII text, so never output any other characters.
```

## Rubric (what a strong answer does)
1. Identifies the conflict between the 60-word limit and the detailed 9-step walkthrough, and resolves it explicitly (e.g. one step at a time, or a clear priority rule) rather than keeping both as absolute requirements
2. Notices that asking for emojis conflicts with the plain-ASCII-only rule, and the revised prompt no longer asks for both
3. The revised prompt keeps the factory-reset warning requirement and the 9 checklist items (or states clearly why any was changed)
4. Briefly explains to the user what was changed and why, so they can check the edits against the complaints

## Answer A
The prompt contradicts itself in three places. That is likely the main cause of the "weird" answers. An 8B model can't resolve conflicting rules well, so it picks one rule at random or tries to half-satisfy both.

## What's broken

1. **"Under 60 words" vs. "full 9-step checklist, each step in detail."** You can't do both. The model either truncates mid-checklist or ignores the word limit.
   **Fix:** Give one step per message, then ask whether it worked. This fits the widget and suits troubleshooting better anyway.

2. **"Add an emoji or two" vs. "plain ASCII only."** Emoji aren't ASCII, so you get garbled characters in the widget, or the model drops the warmth entirely.
   **Fix:** Keep the ASCII rule because it's a hard technical constraint. Get warmth from wording, or allow ASCII smileys like `:)`.

3. **Factory reset (step 8) comes before "Contact ISP" (step 9).** The bot tells users to wipe their config before checking whether the ISP is down. That's the most destructive step placed ahead of the cheapest one.
   **Fix:** Move the ISP check earlier and make factory reset the last resort. It should also need explicit user confirmation.

Smaller issues:
- **Firmware update in the middle of an outage.** A user with no internet usually can't download firmware, and a failed update can brick the router. Move it later or make it conditional.
- **"Always" on the checklist.** Someone whose only problem is a slow Wi-Fi device gets told to check cables and log in to the admin page. Let the bot skip steps the user has already ruled out.
- **No scope or fallback rules.** Nothing says what to do with off-topic questions or unknown answers. Small models then make up settings and menu names.
- **Dense one-line list.** 8B models follow short numbered lines better than a run-on list.

## Improved prompt

```
You are NetBox Support, a friendly assistant for NetBox R7 routers.

OUTPUT RULES (hard limits):
- Plain ASCII text only. No emoji, no special symbols, no curly quotes, no markdown.
- Maximum 60 words per reply.
- Be warm and upbeat through your wording. You may use :) at most once.

CONNECTION PROBLEMS:
Guide the user through the checklist ONE step at a time.
For each step: explain it in 1-2 short sentences, then ask if it fixed the problem.
If yes: congratulate them and stop.
If no: go to the next step.
If the user says they already did a step, skip it.

Checklist:
1. Check that all cables are firmly connected.
2. Restart the router: unplug it, wait 30 seconds, plug it back in.
3. Check the lights on the front of the router and tell me their colors.
4. Check with your internet provider (ISP) for outages in your area.
5. Log in to 192.168.0.1 in a browser and check the WAN settings.
6. Check the DNS settings on the same page.
7. Factory reset (see warning rule below).
8. Contact your ISP or NetBox support for further help.

FACTORY RESET RULE:
Before suggesting a factory reset, you MUST say: "Warning: a factory reset erases ALL your settings, including Wi-Fi name and password."
Then ask the user to confirm before giving reset instructions.

FIRMWARE:
Only suggest a firmware update if the internet is working again, or if the user asks.

LIMITS:
- Only answer questions about NetBox R7 routers.
- If you do not know an answer, say so and suggest contacting NetBox support.
- Never invent menu names, settings, or features.

EXAMPLE:
User: My internet stopped working.
Assistant: Sorry about that! Let's fix it together. First, please check that all cables on the router are firmly plugged in, especially the one from the wall. Did that help?
```

A few notes on the design choices:
- **Rules are grouped under caps headers.** Small models tend to follow sectioned, short-line prompts more reliably than prose.
- **The example sets format and tone through demonstration.** For an 8B model this probably does more than any adjective like "friendly". Check that your examples are pure ASCII and under 60 words, or the model will copy the violation.
- **The reset warning is verbatim text**, so you can check for it automatically.
- **Step 3 asks for the LED colors** but doesn't say what they mean. If you have the R7 LED meanings, add them in a short table. Otherwise the model will guess, and that's a likely source of wrong answers.
- **Double-check the details I added:** the 30-second power-cycle wait and "WAN and DNS on the same page" are my assumptions, not facts from your docs. Replace them with what the R7 actually does. The 192.168.0.1 address comes from your original prompt.

## Check it before shipping

Run 15-20 real user messages from your logs through both the old and new prompt, and score:

| Check | How |
|---|---|
| ASCII only | regex `[^\x00-\x7F]` (should find no matches) |
| Under 60 words | word count |
| One step per reply | manual or LLM judge |
| Reset warning present before reset instructions | string match |
| Stays on topic / admits unknowns | include 3-4 off-topic and "I don't know" cases |

Include edge cases in the test set: a user who says "I already restarted it", a user who asks directly for a factory reset, a non-connection question like "how do I change my Wi-Fi password", and an off-topic question.

Even with this prompt, an 8B model may still sometimes go over 60 words or slip in a non-ASCII character. Enforce both in code as well: strip or replace non-ASCII characters, and truncate or regenerate replies that are too long. Don't rely on the prompt alone for hard widget constraints.


## Answer B
The "weird" answers most likely come from contradictions in the prompt, not from the model itself. A small model like Llama 3.1 8B can't settle a conflict between two rules, so it breaks one of them at random. I found three:

| # | Conflict | What the bot probably does |
|---|----------|----------------------------|
| 1 | "Under 60 words" vs. "walk through all 9 steps, explaining each in detail" | Cuts off mid-list, crams all 9 steps into fragments, or ignores the word limit |
| 2 | "Add an emoji or two" vs. "plain ASCII only" | Outputs emoji that show up as broken characters in the widget |
| 3 | The checklist runs straight into step 8 (factory reset) | Suggests a reset while the user is still on step 2, or leaves out the warning because of the word limit |

There are also some smaller problems:
- **The bot doesn't know the facts it's asked to explain.** It has no LED meanings and no WAN or DNS values, so "explain each step in detail" pushes it to invent them.
- **Nothing covers questions outside the product.** There's no rule for other router models or unrelated questions.
- **The step order is questionable.** Updating firmware (step 6) before checking DNS (step 7) is a slower and riskier step coming before a quick check. That's my view; your support team may have a reason for the order.

## Fix: one step per message

Going one step at a time removes conflict 1. Each message fits in 60 words, and the user reports back before the bot moves on. For conflict 2, use ASCII smileys like `:)` in place of emoji.

## Rewritten prompt

```
You are NetBox Support, a chat assistant for the NetBox R7 router only.

OUTPUT RULES (always follow):
- Maximum 60 words per reply.
- Plain ASCII characters only. No emoji, no special symbols, no curly quotes.
- Tone: friendly and upbeat. You may use a simple ASCII smiley like :) at most once per reply.

CONNECTION PROBLEMS:
Guide the user through the checklist ONE step per reply. Give short instructions for the current step, then ask if it fixed the problem. If yes, stop. If no, go to the next step. Do not list future steps.

Checklist:
1. Check that all cables are firmly connected.
2. Power-cycle the router: unplug it, wait 30 seconds, plug it back in.
3. Check the LED lights and ask the user to describe them.
4. Log in at http://192.168.0.1 in a web browser.
5. Check the WAN settings page.
6. Check the DNS settings.
7. Update the firmware.
8. Factory reset.
9. Contact the internet provider (ISP).

FACTORY RESET RULE:
Before step 8, always say: "Warning: a factory reset erases ALL your settings, including Wi-Fi name and password." Then ask the user to confirm before giving reset instructions.

DO NOT:
- Invent LED meanings, menu names, or setting values. If you do not know, say so and suggest the user contact NetBox support.
- Answer questions about other products. Say politely that you only support the NetBox R7.

EXAMPLE
User: My internet stopped working.
Assistant: Sorry about that! Let's fix it together :) First, please check that the power cable and the internet cable are firmly plugged into the router. Did that fix it?

User: Cables are fine, still no internet.
Assistant: Thanks for checking! Next, unplug the router, wait 30 seconds, then plug it back in. Give it 2 minutes to start up. Is your internet working now?
```

## What changed, and why

- **Hard output rules come first, in a short labeled block.** Small models follow short, clearly labeled rules better than rules scattered through a paragraph. I'm reasoning from general experience with 8B models here, not from a test on your data.
- **One step per turn** keeps every reply under 60 words.
- **The emoji rule is replaced with an ASCII smiley** allowance, capped at one per reply.
- **The reset warning is written out word for word, and the user must confirm.** A small model skips less when it can copy the exact text instead of paraphrasing a rule.
- **The "don't invent" rule** stops the bot from making up LED codes. If you add the real LED table to the prompt (for example, "solid green = connected"), step 3 becomes useful instead of vague.
- **The two example exchanges** show the tone, the length and the one-step pattern. With small models, examples usually work better than descriptions. Watch whether the bot starts repeating the example wording back to users.
- **DNS and firmware are swapped.** Undo this if your support team wants the original order.

## Test it before shipping

Run the old and new prompts on the same set of about 15 to 20 real chat logs, especially the ones users complained about. Check each reply for:

| Check | Pass condition |
|-------|----------------|
| Length | 60 words or fewer |
| Characters | ASCII only (check automatically with a regex such as `[^\x00-\x7F]`) |
| Pacing | One step per reply, and waits for the user |
| Reset safety | Warning shown and confirmation requested before any reset instructions |
| Made-up facts | No invented LED meanings or settings |
| Off-topic | Politely declines non-R7 questions |

Include these edge cases: a user who asks to jump straight to a factory reset, a user who says "I already tried steps 1-3", a question about another router model, and a user writing in another language. The prompt doesn't handle the last one yet, so decide on the behavior you want first.

**Also do this in code:** strip or replace non-ASCII characters in the widget before displaying replies. An 8B model will still break a prompt-only rule now and then, so a code filter is a more reliable guarantee than any wording.

