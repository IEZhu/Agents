I'll give you the numbers below so you have a draft for Monday. But I disagree with the plan to hardcode them without a pharmacist signing off. This table would be the one deterministic safety control in the system. If a value in it is wrong, every guardrail you've built will enforce the wrong value with full confidence. The values also depend on things I can't see from here: your market (UK, US and EU labels differ), your exact SKUs and strengths, and the concentrations of your liquids. A pharmacist can review a pre-filled table in roughly an hour. That's far less work than labelling, and it's the hour that matters most.

## Suggested design change first

Don't let the model produce dose numbers at all, even under a ceiling.

- **Doses come from structured data, not generation.** Extract dose fields per SKU from the leaflets you already index: strength, dose per age band, minimum interval, max doses per 24h. Have a pharmacist sign off on them and version them. When a user asks "how much can I take", the UI renders the dosing table for that specific product. The model's job is to pick the product and handle context ("are you taking anything else?"). It doesn't write the numbers.
- **Children: age bands from the label, not mg/kg arithmetic in chat.** OTC paediatric labels are usually banded by age (sometimes by weight). Weight-based dosing in a chat adds three failure modes: a weight the user misremembers or enters in the wrong unit, a model arithmetic error, and confusion between concentrations (for example 120 mg/5 ml vs 250 mg/5 ml paracetamol suspension). Getting the concentration wrong is the classic cause of a 2x overdose. Show the band table for the exact bottle.
- **Keep an output validator as a backstop.** Use a regex for `\d+\s*(mg|ml|g|tablets?|caplets?)` in any response that mentions these ingredients. If a number isn't in the signed-off table for the product in context, block the response and fall back to the rendered table plus "ask a pharmacist".
- **Route interactions and contraindications to "see pharmacist".** Don't try to adjust the dose. If the user reports any item on the lists below, the answer is not a lower dose. The urgency field goes to "see pharmacist", and the product table is hidden.
- **Add these cases to the eval set:** combination cold/flu products (hidden paracetamol), "I already took some 2 hours ago", a child's weight given in lbs, "can I alternate ibuprofen and paracetamol", pregnancy, and "I drink most nights".

## Draft values (for pharmacist review, not for shipping as-is)

All values below are **recalled, not verified**. They match typical UK/US OTC labelling as I remember it. Confirm each one against your leaflets and your market's labelling.

### Paracetamol (acetaminophen)

| Population | Single dose | Interval | Max / 24h |
|---|---|---|---|
| Adults and 16+ (UK labels; US is 12+) | 500 mg–1 g | ≥4 h | 4 g (UK OTC: 8 × 500 mg). Some US labels cap at 3 g (e.g. 6 × 500 mg). Use your label. |
| Adults <50 kg, liver impairment, regular heavy alcohol use, malnourished | Needs pharmacist input. Clinical guidance often lowers the ceiling (commonly cited ~2–3 g/day or ~60 mg/kg/day) | — | Route to pharmacist |
| Children | Label age bands. As a sanity check only: ~10–15 mg/kg per dose | ≥4–6 h | Max 4 doses/24h (~60 mg/kg/day OTC). Never above the adult dose |

### Ibuprofen

| Population | Single dose | Interval | Max / 24h |
|---|---|---|---|
| Adults and 12+ (OTC) | 200–400 mg | 4–6 h (UK labels often say take 3 times daily) | 1,200 mg OTC. Higher doses are prescription-only and out of scope |
| Children | Label age bands. As a sanity check only: ~5–10 mg/kg per dose | 6–8 h | ~20–30 mg/kg/day, typically 3 doses/day (UK labels). UK: not under 3 months or under 5 kg. Check your label |

### Interactions and contraindications that should route to "see pharmacist"

**Paracetamol**
- **Other paracetamol-containing products.** Cold/flu sachets, co-codamol and similar combinations are the most common route to accidental overdose. Where possible, check against the basket and order history.
- **Regular heavy alcohol use** or **liver disease**: higher hepatotoxicity risk.
- **Warfarin**: regular paracetamol use can raise INR.
- **Enzyme-inducing drugs** (e.g. carbamazepine, phenytoin, rifampicin): may raise hepatotoxicity risk.
- **Any suspected overdose**, including "I took a bit extra": emergency path, always. Paracetamol overdose can look asymptomatic early on. Add this to the red-flag classifier.

**Ibuprofen (NSAIDs)**
- **Anticoagulants** (warfarin, apixaban, rivaroxaban, etc.) and **antiplatelets**, including low-dose aspirin: bleeding risk. Ibuprofen may also reduce aspirin's cardioprotective effect.
- **SSRIs/SNRIs** and **oral corticosteroids**: GI bleeding risk.
- **ACE inhibitors / ARBs + diuretics**: the "triple whammy" risk of acute kidney injury.
- **Lithium, methotrexate**: raised levels and toxicity.
- **Other NSAIDs**, including naproxen and high-dose aspirin.
- **Alcohol**: GI bleeding risk.
- **Conditions:** past stomach ulcer or GI bleed, kidney disease, heart failure, uncontrolled hypertension, asthma that NSAIDs have worsened, pregnancy (avoid, especially from around 20 weeks onward; check your market's current guidance), elderly users. In children: dehydration, and chickenpox (UK guidance advises avoiding).

This list is not exhaustive, and that's another reason a pharmacist should own it rather than me.

## Two things to raise at sprint planning

1. **Regulatory status.** A symptom checker that gives personalised dosing may count as medical device software in some jurisdictions. That's possible in the UK under MHRA and in the EU under MDR, but I'm not certain how it applies to your product. Check with your regulatory or legal contact before this goes live, because it could change the scope more than any ticket will.
2. **Pharmacist time.** Frame the request as "review and sign off a pre-filled table, about 1 hour", not "help build dosing". Pause labelling for that hour if you need to. The eval set measures safety; this table enforces it.
